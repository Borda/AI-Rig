"""Tests for check_bash_persistence bin script.

Covers block extraction, assignment/reference detection, cross-block violation detection, env-var filtering, and CLI
integration.
"""

from __future__ import annotations

from pathlib import Path

import check_bash_persistence as cbp
import pytest


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

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("just prose\n", id="no-fences"),
            pytest.param("```python\nFOO=1\n```\n", id="non-bash-fence"),
        ],
    )
    def test_text_without_bash_fence_returns_empty(self, text: str) -> None:
        """Text with no bash fence yields no blocks.

        Covers a file with no fences at all and one whose only fence is Python, whose assignment must not be mistaken
        for shell state.
        """
        assert cbp.extract_bash_blocks(text) == []


class TestAssignedVars:
    """Covers assigned_vars() variable assignment detection."""

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("FOO=bar\n", id="plain"),
            pytest.param("export FOO=bar\n", id="export-prefix"),
            pytest.param("local FOO=bar\n", id="local-prefix"),
            pytest.param("  FOO=bar\n", id="indented"),
            pytest.param("FOO=$(date)\n", id="command-substitution"),
        ],
    )
    def test_assignment_form_detected(self, line: str) -> None:
        """Every supported VAR=value form is reported as an assignment of FOO.

        Covers the bare form, the export and local prefixes, indentation, and a value produced by command substitution.
        """
        assert "FOO" in cbp.assigned_vars(line)

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("# FOO=bar\n", id="comment-line"),
            pytest.param("echo $FOO\n", id="dollar-reference"),
            pytest.param('[ "$FOO" = "x" ]\n', id="test-expression-reference"),
        ],
    )
    def test_non_assignment_line_yields_no_vars(self, line: str) -> None:
        """A comment or a bare variable reference is not an assignment.

        Covers an assignment inside a comment and references whose ``=`` is not in assignment position.
        """
        assert cbp.assigned_vars(line) == frozenset()


class TestReferencedVars:
    """Covers referenced_vars() variable reference detection."""

    @pytest.mark.parametrize(
        "block",
        [
            pytest.param("echo $FOO\n", id="dollar"),
            pytest.param("echo ${FOO}\n", id="braced"),
            pytest.param("# note $BAR\necho $FOO\n", id="code-line-beside-comment"),
        ],
    )
    def test_reference_form_detected(self, block: str) -> None:
        """A $VAR or ${VAR} reference on a code line is reported, even beside comment lines."""
        assert "FOO" in cbp.referenced_vars(block)

    @pytest.mark.parametrize(
        ("block", "var"),
        [
            pytest.param("echo $HOME\n", "HOME", id="known-env-var"),
            pytest.param("for f in *; do echo $f; done\n", "f", id="single-char-var"),
            pytest.param("# $COMMIT_SENTINEL is gone\n", "COMMIT_SENTINEL", id="full-line-comment"),
        ],
    )
    def test_filtered_reference_not_returned(self, block: str, var: str) -> None:
        """Known env vars, single-character loop vars and names mentioned only in comments are not references."""
        assert var not in cbp.referenced_vars(block)

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

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("```bash\nFOO=1\necho $FOO\n```\n", id="single-block-with-reference"),
            pytest.param("```bash\nFOO=1\n```\n", id="single-block-skipped"),
            pytest.param("```bash\nBAR=x\n```\n```bash\nBAZ=y\necho $BAZ\n```\n", id="assign-and-ref-same-block"),
            pytest.param(
                '```bash\necho start\n```\n```bash\n[ "$LOCAL_MODE" = "true" ]\n```\n', id="never-assigned-env-var"
            ),
        ],
    )
    def test_clean_file_reports_no_findings(self, tmp_path: Path, content: str) -> None:
        """Files with no cross-block variable loss produce no findings.

        Covers a single block (with or without a reference, which leaves no cross-block issue possible), a variable
        assigned and used within the same block, and a variable referenced but never assigned anywhere, which is an
        environment variable rather than lost state.
        """
        assert cbp.check_file(_skill(tmp_path, content)) == []

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

    def test_var_assigned_in_block2_referenced_in_block3_flagged(self, tmp_path: Path) -> None:
        """Assign in block 2, reference in block 3 is flagged with correct block numbers."""
        content = "```bash\necho a\n```\n```bash\nTS=$(date)\n```\n```bash\necho $TS\n```\n"
        f = _skill(tmp_path, content)
        findings = cbp.check_file(f)
        assert any("block 2" in x and "block 3" in x for x in findings)

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing file returns no findings instead of raising."""
        assert cbp.check_file(tmp_path / "missing.md") == []


class TestTemplateBlock:
    """Covers is_template_block() placeholder detection (suppression rule 3)."""

    @pytest.mark.parametrize(
        ("block", "assigned"),
        [
            pytest.param("cp x .../ctx-${I}.md\n", frozenset({"RUN_ID"}), id="unassigned-loop-counter"),
            pytest.param('grep "$SQ" rdeps <TARGET_MODULE>\n', frozenset({"SQ"}), id="angle-bracket-placeholder"),
        ],
    )
    def test_placeholder_block_flags_template(self, block: str, assigned: frozenset[str]) -> None:
        """A block carrying a never-assigned token or an <identifier> placeholder is a template.

        Covers a ``${I}`` loop-counter token no block assigns and an angle-bracket usage-example placeholder.
        """
        assert cbp.is_template_block(block, assigned) is True

    @pytest.mark.parametrize(
        ("block", "assigned"),
        [
            pytest.param("echo ${RUN_ID}\n", frozenset({"RUN_ID"}), id="all-tokens-assigned"),
            pytest.param('echo "$ARGUMENTS"\n', frozenset(), id="known-env-var"),
            pytest.param("# uses ${I}\necho done\n", frozenset(), id="placeholder-only-in-comment"),
            pytest.param('sort < "$INFILE"\n', frozenset({"INFILE"}), id="redirection-from-file"),
            pytest.param('diff <(sort "$A") "$B"\n', frozenset({"A", "B"}), id="process-substitution"),
        ],
    )
    def test_ordinary_block_is_not_template(self, block: str, assigned: frozenset[str]) -> None:
        """Assigned refs, known env vars, comment-only tokens and real shell redirection do not mark a template.

        The redirection cases (`< file`, `<(`) must not be mistaken for an angle-bracket placeholder.
        """
        assert cbp.is_template_block(block, assigned) is False


class TestReloadsBeforeRef:
    """Covers reloads_before_ref() state-reload detection (suppression rule 1)."""

    @pytest.mark.parametrize(
        ("block", "var", "expected"),
        [
            pytest.param('eval "$(git_slugs.sh)"\nrm -f "$SENTINEL"\n', "SENTINEL", True, id="eval-reload"),
            pytest.param('source ./state.sh\necho "$VARX"\n', "VARX", True, id="source-reload"),
            pytest.param(
                '. "${TMPDIR:-/tmp}/state-${CSID}"\necho "$VARX"\n', "VARX", True, id="dot-reload-quoted-path"
            ),
            pytest.param('echo "$VARX"\neval "$(gen)"\n', "VARX", False, id="reload-after-reference"),
            pytest.param('echo "$VARX"\n', "VARX", False, id="no-reload"),
        ],
    )
    def test_reload_must_precede_reference(self, block: str, var: str, expected: bool) -> None:
        """Only an eval, source or dot-source placed before the reference re-derives the value.

        A reload appearing after the reference does not rescue it, and a plain reference with no reload is not
        suppressed.
        """
        assert cbp.reloads_before_ref(block, var) is expected


class TestRefsAllDefended:
    """Covers refs_all_defended() empty-var defence detection (suppression rule 2)."""

    @pytest.mark.parametrize(
        ("block", "var", "expected"),
        [
            pytest.param(
                '_SKILLS="${_SHARED%/_shared}"\n[ -z "$_SKILLS" ] && _SKILLS="fallback"\n',
                "_SHARED",
                True,
                id="strip-assignment-with-guard",
            ),
            pytest.param('echo "${OUTDIR:-/tmp}"\n', "OUTDIR", True, id="default-expansion"),
            pytest.param('echo "$OUTDIR"\n', "OUTDIR", False, id="bare-reference"),
            pytest.param('X="${A:-y}"\necho "$A"\n', "A", False, id="partial-defence"),
        ],
    )
    def test_refs_all_defended_requires_every_reference_guarded(self, block: str, var: str, expected: bool) -> None:
        """A variable is defended only when every reference guards against an empty value.

        A guarded strip-assignment and a ``${VAR:-default}`` expansion defend; a bare reference does not, and neither
        does one defended reference beside a bare one.
        """
        assert cbp.refs_all_defended(block, var) is expected


class TestSuppressionEndToEnd:
    """Covers check_file() suppression of the three FP classes plus real-loss preservation."""

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(
                '```bash\nSENTINEL=/tmp/x\n```\n```bash\neval "$(gen_slugs)"\nrm -f "$SENTINEL"\n```\n',
                id="state-reload",
            ),
            pytest.param(
                "```bash\nSENTINEL=/tmp/x\n```\n```bash\n# $SENTINEL gone\necho done\n```\n",
                id="comment-only-reference",
            ),
            pytest.param(
                "```bash\n_SHARED=/a/b/_shared\n```\n"
                '```bash\n_SKILLS="${_SHARED%/_shared}"\n[ -z "$_SKILLS" ] && _SKILLS="x"\n```\n',
                id="empty-var-defended",
            ),
            pytest.param(
                "```bash\nRUN_ID=$(date -u +%s)\n```\n```bash\ngit log > state/${RUN_ID}/ctx-${I}.md\n```\n",
                id="template-placeholder-block",
            ),
        ],
    )
    def test_false_positive_class_is_suppressed(self, tmp_path: Path, content: str) -> None:
        """Cross-block references that are not real state loss are not flagged.

        Covers the three suppression classes (a state-file reload in the referencing block, a variable named only in a
        comment, an empty-value-guarded reference) plus a block holding a never-assigned ``${I}`` placeholder.
        """
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

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param("```bash\nFOO=1\necho $FOO\n```\n", 0, id="clean-file"),
            pytest.param("```bash\nFOO=1\n```\n```bash\necho $FOO\n```\n", 1, id="cross-block-violation"),
        ],
    )
    def test_scan_dir_exit_code_mirrors_findings(self, tmp_path: Path, content: str, expected: int) -> None:
        """A scanned tree exits 0 when clean and 1 when a cross-block reference is found."""
        _skill(tmp_path, content)
        assert cbp.main(["--scan-dir", str(tmp_path)]) == expected

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

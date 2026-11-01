"""Tests for the check_bash_persistence bin script.

Covers block extraction, assignment/reference detection, cross-block violation detection, the suppression of look-alikes
that are not real state loss, env-var filtering, and CLI integration.
"""

from __future__ import annotations

from pathlib import Path

import check_bash_persistence as cbp
import pytest

_FENCE = "```"


def _md(*blocks: str) -> str:
    """Return Markdown holding one ``bash`` fenced block per body in ``blocks``, in order.

    Examples:
        >>> _md("A=1", "echo $A")
        '```bash\\nA=1\\n```\\n```bash\\necho $A\\n```\\n'
    """
    return "".join(f"{_FENCE}bash\n{body}\n{_FENCE}\n" for body in blocks)


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
    """Block boundary detection in ``extract_bash_blocks()``."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(_md("FOO=1"), ["FOO=1\n"], id="single-block"),
            pytest.param("```bash\nA=1\n```\nprose\n```bash\nB=2\n```\n", ["A=1\n", "B=2\n"], id="two-blocks-in-order"),
            pytest.param("just prose\n", [], id="no-fences"),
            pytest.param("```python\nFOO=1\n```\n", [], id="non-bash-fence"),
        ],
    )
    def test_returns_bash_block_bodies_in_document_order(self, text: str, expected: list[str]) -> None:
        """Each bash fence yields its body in document order; text without a bash fence yields no blocks.

        The no-fence and Python-fence cases show that an assignment outside shell code must not be mistaken for shell
        state, and the two-block case that the prose between fences is dropped.
        """
        assert cbp.extract_bash_blocks(text) == expected


class TestAssignedVars:
    """Variable assignment detection in ``assigned_vars()``."""

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
    def test_assignment_form_is_detected(self, line: str) -> None:
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

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param('IFS= read -r RUN_DIR < "$F" 2>/dev/null || RUN_DIR=""\n', {"RUN_DIR"}, id="sentinel-read"),
            pytest.param("while IFS= read -r SRC DEST; do\n", {"SRC", "DEST"}, id="while-read-multiple-names"),
            pytest.param("read -t 5 -a ITEMS\n", {"ITEMS"}, id="read-array-after-arg-flag"),
            pytest.param("mapfile -t FILES < <(ls)\n", {"FILES"}, id="mapfile-strip-flag-no-arg"),
            pytest.param("readarray -t LINES < list.txt\n", {"LINES"}, id="readarray"),
            pytest.param("for PLUGIN in $LIST; do\n", {"PLUGIN"}, id="for-loop-var"),
            pytest.param("A=1; B=2\n", {"A", "B"}, id="semicolon-joined"),
            pytest.param('case "$K" in SINCE) SINCE=$V;; esac\n', {"SINCE"}, id="case-branch"),
            pytest.param('[ -f "$S" ] && ROOT="$(cat "$S")"\n', {"ROOT"}, id="after-and-list"),
        ],
    )
    def test_shell_form_counts_as_assignment(self, line: str, expected: set[str]) -> None:
        """Every builtin or inline form that sets a variable is reported as an assignment of it.

        Covers assignment forms beyond line-start ``VAR=``: builtin targets, loop variables and inline assignments. A
        sentinel read-back left undetected would make the variable look never-assigned, and a correct reload invisible.
        """
        assert expected <= cbp.assigned_vars(line)


class TestReferencedVars:
    """Variable reference detection in ``referenced_vars()``."""

    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            pytest.param("echo $FOO\n", {"FOO"}, id="dollar"),
            pytest.param("echo ${FOO}\n", {"FOO"}, id="braced"),
            pytest.param("# note $BAR\necho $FOO\n", {"FOO"}, id="code-line-beside-comment"),
            pytest.param("cp $SRC $DEST\n", {"SRC", "DEST"}, id="several-on-one-line"),
        ],
    )
    def test_reference_form_is_detected(self, block: str, expected: set[str]) -> None:
        """A $VAR or ${VAR} reference on a code line is reported, even beside comment lines or other references."""
        assert expected <= cbp.referenced_vars(block)

    @pytest.mark.parametrize(
        ("block", "var"),
        [
            pytest.param("echo $HOME\n", "HOME", id="known-env-var"),
            pytest.param("for f in *; do echo $f; done\n", "f", id="single-char-var"),
            pytest.param("# $COMMIT_SENTINEL is gone\n", "COMMIT_SENTINEL", id="full-line-comment"),
        ],
    )
    def test_filtered_reference_is_not_returned(self, block: str, var: str) -> None:
        """Known env vars, single-character loop vars and names mentioned only in comments are not references."""
        assert var not in cbp.referenced_vars(block)

    def test_empty_block_returns_empty(self) -> None:
        """An empty block yields no references."""
        assert cbp.referenced_vars("") == frozenset()


class TestCheckFile:
    """End-to-end violation detection in ``check_file()``."""

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(_md("FOO=1\necho $FOO"), id="single-block-with-reference"),
            pytest.param(_md("FOO=1"), id="single-block-skipped"),
            pytest.param(_md("BAR=x", "BAZ=y\necho $BAZ"), id="assign-and-ref-same-block"),
            pytest.param(_md("echo start", '[ "$LOCAL_MODE" = "true" ]'), id="never-assigned-env-var"),
        ],
    )
    def test_clean_file_reports_no_findings(self, tmp_path: Path, content: str) -> None:
        """Files with no cross-block variable loss produce no findings.

        Covers a single block (with or without a reference, which leaves no cross-block issue possible), a variable
        assigned and used within the same block, and a variable referenced but never assigned anywhere, which is an
        environment variable rather than lost state.
        """
        assert cbp.check_file(_skill(tmp_path, content)) == []

    @pytest.mark.parametrize(
        ("blocks", "var", "assigned_in", "referenced_in"),
        [
            pytest.param(("FOO=bar", "echo $FOO"), "FOO", 1, 2, id="adjacent-blocks"),
            pytest.param(("echo a", "TS=$(date)", "echo $TS"), "TS", 2, 3, id="later-blocks"),
        ],
    )
    def test_cross_block_loss_is_flagged_with_block_numbers(
        self, tmp_path: Path, blocks: tuple[str, ...], var: str, assigned_in: int, referenced_in: int
    ) -> None:
        """A variable assigned in one block and referenced in a later one is flagged C41 with both block numbers.

        Each bash block runs in a fresh shell, so the reference sees an empty value. The report must name the variable
        and point at the assigning and referencing blocks so the author can find both.
        """
        findings = cbp.check_file(_skill(tmp_path, _md(*blocks)))
        assert len(findings) == 1
        assert all(
            text in findings[0] for text in ("C41-CRITICAL", var, f"block {assigned_in}", f"block {referenced_in}")
        )

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        """A missing file returns no findings instead of raising."""
        assert cbp.check_file(tmp_path / "missing.md") == []


class TestStripUnexpanded:
    """Removal of text the shell never expands, in ``strip_unexpanded()``."""

    @pytest.mark.parametrize(
        ("block", "dropped"),
        [
            pytest.param("jq --arg c x '.[$c] | $perms' \"$KEEP\"\n", "$perms", id="single-quoted-jq-program"),
            pytest.param("jq '\n  .a | $cmd\n' \"$KEEP\"\n", "$cmd", id="multi-line-single-quote"),
            pytest.param("echo $KEEP  # don't $GONE\n", "$GONE", id="trailing-comment-apostrophe"),
        ],
    )
    def test_unexpanded_text_is_blanked(self, block: str, dropped: str) -> None:
        """Single-quoted spans and comments vanish while the expanded reference beside them survives.

        Line count is preserved so reference line indices stay aligned with the original block.
        """
        out = cbp.strip_unexpanded(block)
        assert (dropped not in out, "$KEEP" in out, out.count("\n")) == (True, True, block.count("\n"))

    @pytest.mark.parametrize(
        "block",
        [
            pytest.param('echo "it\'s $KEEP"\n', id="apostrophe-inside-double-quotes"),
            pytest.param('echo "${#ARR[@]}" $KEEP\n', id="hash-in-parameter-expansion"),
            pytest.param("echo \\' $KEEP\n", id="escaped-apostrophe"),
        ],
    )
    def test_quote_lookalike_does_not_open_span(self, block: str) -> None:
        """An apostrophe inside double quotes, an escaped one, or ``#`` inside an expansion never hides what follows."""
        assert "$KEEP" in cbp.strip_unexpanded(block)


class TestReloadsBeforeRef:
    """State-reload detection in ``reloads_before_ref()`` (suppression rule 1)."""

    @pytest.mark.parametrize(
        ("block", "var", "expected"),
        [
            pytest.param('eval "$(git_slugs.sh)"\nrm -f "$SENTINEL"\n', "SENTINEL", True, id="eval-reload"),
            pytest.param('source ./state.sh\necho "$VARX"\n', "VARX", True, id="source-reload"),
            pytest.param(
                '. "${TMPDIR:-/tmp}/state-${CSID}"\necho "$VARX"\n', "VARX", True, id="dot-reload-quoted-path"
            ),
            pytest.param('[ -f "$FLAGS" ] && . "$FLAGS"\necho "$VARX"\n', "VARX", True, id="dot-reload-after-and-list"),
            pytest.param('echo "$VARX"\neval "$(gen)"\n', "VARX", False, id="reload-after-reference"),
            pytest.param('echo "$VARX"\n', "VARX", False, id="no-reload"),
        ],
    )
    def test_reload_must_precede_reference(self, block: str, var: str, expected: bool) -> None:
        """Only an eval, source or dot-source placed before the reference re-derives the value.

        A reload may follow ``&&`` on a guard line. A reload appearing after the reference does not rescue it, and a
        plain reference with no reload is not suppressed.
        """
        assert cbp.reloads_before_ref(block, var) is expected


class TestRefsAllDefended:
    """Empty-var defence detection in ``refs_all_defended()`` (suppression rule 2)."""

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
    def test_every_reference_must_be_guarded(self, block: str, var: str, expected: bool) -> None:
        """A variable is defended only when every reference guards against an empty value.

        A guarded strip-assignment and a ``${VAR:-default}`` expansion defend; a bare reference does not, and neither
        does one defended reference beside a bare one.
        """
        assert cbp.refs_all_defended(block, var) is expected


class TestSuppressionEndToEnd:
    """``check_file()`` suppression of the false-positive classes, and preservation of real losses."""

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(_md("SENTINEL=/tmp/x", 'eval "$(gen_slugs)"\nrm -f "$SENTINEL"'), id="state-reload"),
            pytest.param(_md("SENTINEL=/tmp/x", "# $SENTINEL gone\necho done"), id="comment-only-reference"),
            pytest.param(
                _md("_SHARED=/a/b/_shared", '_SKILLS="${_SHARED%/_shared}"\n[ -z "$_SKILLS" ] && _SKILLS="x"'),
                id="empty-var-defended",
            ),
            pytest.param(
                _md(
                    'RUN_DIR_X=/a\necho "$RUN_DIR_X" > "$S"',
                    'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\n'
                    'IFS= read -r RUN_DIR_X < "${TMPDIR:-/tmp}/r-${CSID}" || RUN_DIR_X=""\nls "$RUN_DIR_X"',
                ),
                id="csid-preamble-with-sentinel-read",
            ),
            pytest.param(
                _md(
                    "DIRECT_MODE=false",
                    '[ -f "${TMPDIR:-/tmp}/flags" ] && . "${TMPDIR:-/tmp}/flags"\necho "$DIRECT_MODE"',
                ),
                id="dot-reload-after-and-list",
            ),
            pytest.param(
                _md("cmd=x", "jq --arg x y '.[] | select(.c == $cmd)' f.json"), id="single-quoted-jq-variable"
            ),
        ],
    )
    def test_false_positive_class_is_suppressed(self, tmp_path: Path, content: str) -> None:
        """Cross-block references that are not real state loss are not flagged.

        Covers the suppression classes (a state-file reload in the referencing block, a variable named only in a
        comment, an empty-value-guarded reference), a sentinel read-back beside the standard CSID preamble, a dot-source
        guarded by ``&&``, and a jq program variable inside single quotes, which the shell never expands.
        """
        assert cbp.check_file(_skill(tmp_path, content)) == []

    @pytest.mark.parametrize(
        ("content", "lost"),
        [
            pytest.param(
                _md("RUN_ID=$(date -u +%s)", "git log > state/${RUN_ID}/ctx-${I}.md"),
                "RUN_ID",
                id="never-assigned-placeholder-beside-lost-var",
            ),
            pytest.param(
                _md("PLUGIN_ROOT=/p", 'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\nnode "$PLUGIN_ROOT/hooks/x.js"'),
                "PLUGIN_ROOT",
                id="csid-preamble-beside-lost-var",
            ),
            pytest.param(
                _md("PYTEST_CMD=pytest", 'eval "$PYTEST_CMD --tb=short <test_file>::test_<bug> -v"'),
                "PYTEST_CMD",
                id="angle-placeholder-beside-lost-var",
            ),
            pytest.param(
                _md("PLUGIN_ROOT=/p", "jq --arg c x '.[$cmd]' f > g\necho \"$PLUGIN_ROOT\""),
                "PLUGIN_ROOT",
                id="jq-program-beside-lost-var",
            ),
            pytest.param(_md("MEMORY_DIR=/a/b", 'ls "$MEMORY_DIR"/x-*.md'), "MEMORY_DIR", id="bare-loss"),
            pytest.param(_md("VARX=1", 'echo "$VARX"\neval "$(gen)"'), "VARX", id="reload-placed-after-reference"),
        ],
    )
    def test_real_loss_is_flagged_beside_look_alikes(self, tmp_path: Path, content: str, lost: str) -> None:
        """Exemption is per variable: a never-assigned token in a block never masks a real lost variable beside it.

        Covers a ``${I}`` placeholder, the standard CSID preamble (``$PPID``/``$CLAUDE_CODE_SESSION_ID``), an
        ``<identifier>`` placeholder and a single-quoted jq program, each of which used to exempt the whole block. A
        bare cross-block reference, and one whose reload comes only after the reference, still flag with no look-alike
        beside them.
        """
        findings = cbp.check_file(_skill(tmp_path, content))
        assert len(findings) == 1
        assert f"${lost} " in findings[0]


class TestMain:
    """``main()`` CLI integration."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param(_md("FOO=1\necho $FOO"), 0, id="clean-file"),
            pytest.param(_md("FOO=1", "echo $FOO"), 1, id="cross-block-violation"),
        ],
    )
    def test_scan_dir_exit_code_mirrors_findings(self, tmp_path: Path, content: str, expected: int) -> None:
        """A scanned tree exits 0 when clean and 1 when a cross-block reference is found."""
        _skill(tmp_path, content)
        assert cbp.main(["--scan-dir", str(tmp_path)]) == expected

    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param("myplugin/skills/myskill/modes/mode.md", id="mode-file"),
            pytest.param("myplugin/skills/_shared/shared.md", id="shared-doc"),
            pytest.param("myplugin/skills/myskill/templates/t.md", id="template"),
            pytest.param("myplugin/agents/helper.md", id="agent-prompt"),
            pytest.param("myplugin/claude-skills/advise/SKILL.md", id="claude-skill"),
        ],
    )
    def test_scan_dir_reaches_files_beyond_the_skill_entry_point(self, tmp_path: Path, relative: str) -> None:
        """A cross-block loss in a mode, shared doc, template, agent prompt or claude-skill is found by ``--scan-dir``.

        Those files' bash blocks run as separate Bash calls exactly like ``SKILL.md``'s, but a scan limited to
        ``*/skills/*/SKILL.md`` left every one of them unchecked.
        """
        target = tmp_path / relative
        target.parent.mkdir(parents=True)
        target.write_text(_md("FOO=1", "echo $FOO"), encoding="utf-8")
        assert cbp.main(["--scan-dir", str(tmp_path)]) == 1

    def test_scan_dir_ignores_markdown_outside_skill_and_agent_trees(self, tmp_path: Path) -> None:
        """A README with the same block shape is documentation, not an executed skill, so it is not scanned."""
        target = tmp_path / "myplugin" / "docs" / "guide.md"
        target.parent.mkdir(parents=True)
        target.write_text(_md("FOO=1", "echo $FOO"), encoding="utf-8")
        assert cbp.main(["--scan-dir", str(tmp_path)]) == 0

    def test_explicit_file_argument_is_checked(self, tmp_path: Path) -> None:
        """An explicit file path argument is checked and its cross-block loss exits 1.

        The pre-commit hook passes changed files by name, so a path outside any scanned tree must still be examined.
        """
        f = _skill(tmp_path, _md("FOO=1", "echo $FOO"))
        assert cbp.main([str(f)]) == 1

    def test_timeout_flag_is_accepted(self, tmp_path: Path) -> None:
        """The --timeout flag is accepted without error on a clean tree.

        Callers pass a per-file time budget; an unknown-option exit here would break every audit run that sets it.
        """
        _skill(tmp_path, _md("FOO=1\necho $FOO"))
        assert cbp.main(["--scan-dir", str(tmp_path), "--timeout", "15"]) == 0

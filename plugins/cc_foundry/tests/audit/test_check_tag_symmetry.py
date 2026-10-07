"""Tests for check_tag_symmetry bin script.

Covers empty-block detection, unbalanced tag detection, escaped-tag detection, the per-subcheck ``--check`` selector,
clean files, and CLI integration.
"""

from __future__ import annotations

from pathlib import Path

import check_tag_symmetry as cts
import pytest


def _messages(findings: list[cts.Finding]) -> list[str]:
    """Return the message text of each finding, for substring assertions.

    Args:
        findings: Findings returned by ``check_file``.

    Returns:
        One message string per finding, in the original order.

    Examples:
        >>> _messages([cts.Finding(cts.FindingKind.UNBALANCED, "bad")])
        ['bad']
    """
    return [f.message for f in findings]


class TestCheckFile:
    """Covers check_file() for individual file scenarios."""

    def test_empty_block_detected(self, tmp_path: Path) -> None:
        """Empty structural block returns one violation."""
        f = tmp_path / "bad.md"
        f.write_text("<objective></objective>\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert len(violations) == 1
        assert "empty block <objective></objective>" in violations[0]

    @pytest.mark.parametrize(
        ("text", "kind", "tag"),
        [
            pytest.param("<notes>   </notes>\n", "empty block", "<notes>", id="whitespace-only-block"),
            pytest.param("<constants>\n\n</constants>\n", "empty block", "constants", id="blank-lines-only-block"),
            pytest.param("<workflow>\ncontent\n", "unbalanced", "<workflow>", id="open-without-close"),
            pytest.param("content\n</inputs>\n", "unbalanced", "<inputs>", id="close-without-open"),
        ],
    )
    def test_defective_block_is_flagged(self, tmp_path: Path, text: str, kind: str, tag: str) -> None:
        """An empty or unbalanced structural block yields a finding naming the defect and the tag.

        Covers a block holding only spaces, one holding only blank lines (no code fence to count as content), a tag
        opened and never closed, and a closing tag with no opening.
        """
        f = tmp_path / "bad.md"
        f.write_text(text, encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert any(kind in v and tag in v for v in violations)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("<objective>\ncontent\n</objective>\n", id="balanced-non-empty-block"),
            # Tags outside the structural registry are never checked.
            pytest.param("<example></example>\n<code></code>\n", id="non-structural-tags-ignored"),
            # Tag names quoted inside an HTML comment must not inflate the open count.
            pytest.param(
                "<!-- Tag convention: <role>, <workflow>, <notes> are structural. -->\n"
                "<role>\ncontent\n</role>\n"
                "<workflow>\ncontent\n</workflow>\n"
                "<notes>\ncontent\n</notes>\n",
                id="tag-names-in-html-comment",
            ),
            # A code fence counts as content, so the block is not empty.
            pytest.param("<constants>\n\n```yaml\nKEY: value\n```\n\n</constants>\n", id="block-holding-only-a-fence"),
            pytest.param("<routing-boundaries>\ncontent\n</routing-boundaries>\n", id="hyphenated-tag-name"),
            # Only whole-line tags are structural; `<a_b>` inside a sentence is a placeholder.
            pytest.param("<role>\nWrite to <output_path> when done.\n</role>\n", id="underscore-placeholder-in-prose"),
            # A four-backtick template fence hides its inner three-backtick block too.
            pytest.param(
                "<role>\ncontent\n</role>\n\n````\n**Run:**\n```\n<pytest_cmd>\n```\n````\n",
                id="underscore-tag-inside-wide-fence",
            ),
            # Collapsible README sections open with attributes but close with a bare tag.
            pytest.param(
                "<details open>\n<summary>Title</summary>\n\nbody\n\n</details>\n", id="attributed-open-bare-close"
            ),
            # A `<name>` seen only mid-sentence is a placeholder, never a discovered block.
            pytest.param("Pass <output-path> to the script, then read <output-path>.\n", id="placeholder-only-inline"),
            # Stripping comments before code spans would shift later span pairing and leak the quoted `<workflow>` tag.
            pytest.param(
                "The marker `<!-- policy-sibling: a.md, b.md -->` is required; "
                "see the curator `<workflow>` step for the follow-up.\n",
                id="code-span-quoting-html-comment",
            ),
        ],
    )
    def test_well_formed_text_returns_empty(self, tmp_path: Path, text: str) -> None:
        """Well-formed structural markup, and text only resembling it, returns no violations.

        Covers balanced non-empty blocks, tags outside the registry, tag names quoted in HTML comments or code spans,
        fence-only content, hyphenated names, underscore placeholders in prose or fences, attributed open tags and mid-
        sentence placeholders.
        """
        f = tmp_path / "ok.md"
        f.write_text(text, encoding="utf-8")
        assert cts.check_file(f) == []

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(
                "<role>\ncontent\n</role>\nUse `\\<notes>` to suppress navigation.\n", id="inline-backtick-span"
            ),
            pytest.param("<role>\ncontent\n</role>\n```markdown\n\\<workflow>\nexample\n```\n", id="fenced-code-block"),
        ],
    )
    def test_escaped_tag_in_code_is_not_flagged(self, tmp_path: Path, text: str) -> None:
        """A backslash-escaped structural tag inside an inline backtick span or a fenced block is not flagged."""
        f = tmp_path / "escaped.md"
        f.write_text(text, encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert not any("escaped structural tag" in v for v in violations)

    def test_unreadable_file_returns_error(self, tmp_path: Path) -> None:
        """Non-existent path returns cannot-read violation instead of raising."""
        fake = tmp_path / "ghost.md"
        result = cts.check_file(fake)
        assert len(result) == 1
        assert result[0].kind is cts.FindingKind.READ_ERROR
        assert "cannot read" in result[0].message

    def test_oversized_file_returns_read_error_without_being_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file over the size guard is reported as a READ_ERROR, never loaded into memory.

        The guard is exercised by lowering ``_MAX_FILE_SIZE`` rather than writing a real 10 MB fixture — the size
        comparison is the behavior under test, not the literal threshold.
        """
        monkeypatch.setattr(cts, "_MAX_FILE_SIZE", 8)
        f = tmp_path / "huge.md"
        f.write_text("<objective></objective>\n", encoding="utf-8")
        result = cts.check_file(f)
        assert len(result) == 1
        assert result[0].kind is cts.FindingKind.READ_ERROR
        assert "exceeds" in result[0].message

    def test_multiple_tags_each_violation_reported(self, tmp_path: Path) -> None:
        """File with two empty blocks returns two violations."""
        f = tmp_path / "multi.md"
        f.write_text("<objective></objective>\n<notes></notes>\n", encoding="utf-8")
        violations = cts.check_file(f)
        assert len(violations) == 2

    def test_underscore_tag_name_flagged_with_hyphen_suggestion(self, tmp_path: Path) -> None:
        """A block tag whose name carries an underscore is not a CommonMark tag."""
        f = tmp_path / "underscore.md"
        f.write_text("<routing_boundaries>\ncontent\n</routing_boundaries>\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert any("underscore in structural tag <routing_boundaries>" in v for v in violations)
        assert any("<routing-boundaries>" in v for v in violations)

    def test_underscore_check_is_not_limited_to_the_registry(self, tmp_path: Path) -> None:
        """A name nobody registered still breaks the same way, so it is still flagged."""
        f = tmp_path / "novel.md"
        f.write_text("<never_registered>\ncontent\n</never_registered>\n", encoding="utf-8")
        kinds = [v.kind for v in cts.check_file(f)]
        assert cts.FindingKind.UNDERSCORE_TAG in kinds

    def test_escaped_structural_tag_flagged_as_low(self, tmp_path: Path) -> None:
        """Backslash-escaped structural tag in prose is flagged with [low] severity."""
        f = tmp_path / "escaped.md"
        f.write_text(
            "<role>\ncontent\n</role>\nProse mentioning \\<antipatterns-to-flag> should be flagged.\n",
            encoding="utf-8",
        )
        violations = _messages(cts.check_file(f))
        assert any("escaped structural tag" in v and "antipatterns-to-flag" in v for v in violations)
        assert any("[low]" in v for v in violations)

    @pytest.mark.parametrize(
        "tag",
        [
            "objective",
            "workflow",
            "inputs",
            "notes",
            "constants",
            "calibration",
            "not-for",
            "role",
            "initialization",
            "antipatterns-to-flag",
            "core-knowledge",
        ],
    )
    def test_all_structural_tags_covered(self, tmp_path: Path, tag: str) -> None:
        """Every structural tag name triggers an empty-block finding when empty."""
        f = tmp_path / "tag.md"
        f.write_text(f"<{tag}></{tag}>\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert any("empty block" in v for v in violations)

    def test_every_finding_carries_its_kind(self, tmp_path: Path) -> None:
        """A file violating all three modes yields one finding of each kind."""
        f = tmp_path / "all.md"
        f.write_text(
            "<objective></objective>\n<workflow>\nProse with \\<notes> escaped.\n",
            encoding="utf-8",
        )
        kinds = {finding.kind for finding in cts.check_file(f)}
        assert kinds == {
            cts.FindingKind.EMPTY_BLOCK,
            cts.FindingKind.UNBALANCED,
            cts.FindingKind.ESCAPED_TAG,
        }


class TestDynamicBalance:
    """Covers balance checking of tag names discovered in the file rather than registered."""

    def test_unregistered_block_duplicated_by_prose_fusion_is_flagged(self, tmp_path: Path) -> None:
        """A block whose opening tag also survives inside a prose paragraph is unbalanced.

        This is the shape a paragraph-fusion repair leaves behind: the original tag had been
        swallowed into the intro paragraph, and separating it out re-emitted the tag without
        removing the swallowed copy, so two opens face a single close. The name belongs to no
        registry, which is precisely why the registry-driven check reported the file clean.
        """
        f = tmp_path / "reference.md"
        f.write_text(
            "Intro sentence naming the block. <split-strategies>\n"
            "\n"
            "<split-strategies>\n"
            "\n"
            "content\n"
            "\n"
            "</split-strategies>\n",
            encoding="utf-8",
        )
        violations = _messages(cts.check_file(f))
        assert len(violations) == 1
        assert "unbalanced <split-strategies> — 2 open, 1 close" in violations[0]

    def test_legacy_underscore_block_is_both_renamed_and_balance_checked(self, tmp_path: Path) -> None:
        """A legacy underscore block reports the rename and its balance defect together.

        Underscore names are on the way out, but one that is still in the tree can still be mis-paired. Admitting them
        to discovery means the balance defect is reported in the same run as the rename advice, instead of waiting for
        the rename to land first.
        """
        f = tmp_path / "legacy.md"
        f.write_text("<legacy_block>\n\ncontent\n\n<legacy_block>\n\n</legacy_block>\n", encoding="utf-8")
        findings = cts.check_file(f)
        kinds = {finding.kind for finding in findings}
        assert kinds == {cts.FindingKind.UNDERSCORE_TAG, cts.FindingKind.UNBALANCED}
        assert any("unbalanced <legacy_block> — 2 open, 1 close" in m for m in _messages(findings))


class TestParseKinds:
    """Covers parse_kinds() selector parsing."""

    def test_all_selectable_kinds_parse(self) -> None:
        """The default spec resolves to every selectable kind."""
        spec = ",".join(k.value for k in cts.SELECTABLE_KINDS)
        assert cts.parse_kinds(spec) == set(cts.SELECTABLE_KINDS)

    def test_whitespace_and_case_tolerated(self) -> None:
        """Tokens are trimmed and lower-cased before lookup."""
        assert cts.parse_kinds(" Empty-Block , UNBALANCED ") == {
            cts.FindingKind.EMPTY_BLOCK,
            cts.FindingKind.UNBALANCED,
        }

    @pytest.mark.parametrize(
        ("spec", "match"),
        [
            pytest.param("read-error", "read-error", id="read-error-not-selectable"),
            pytest.param("empty-block,bogus", "bogus", id="unknown-token"),
        ],
    )
    def test_unselectable_mode_raises_naming_the_token(self, spec: str, match: str) -> None:
        """A mode that cannot be selected raises ValueError naming it.

        ``read-error`` is always emitted and never nameable in ``--check``, and an unrecognised token raises with the
        token in the message.
        """
        with pytest.raises(ValueError, match=match):
            cts.parse_kinds(spec)


class TestMain:
    """Covers main() CLI integration."""

    def test_no_files_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Accept an empty file list and report a passing result."""
        rc = cts.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "no files provided" in out

    def test_clean_file_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Single clean file exits 0 with pass line."""
        f = tmp_path / "clean.md"
        f.write_text("<objective>\ncontent\n</objective>\n", encoding="utf-8")
        rc = cts.main([str(f)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "✓" in out

    def test_violation_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Report an empty tag block with the stable ``C14a`` diagnostic identifier."""
        f = tmp_path / "bad.md"
        f.write_text("<constants></constants>\n", encoding="utf-8")
        rc = cts.main([str(f)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "! C14a:" in out

    def test_nonexistent_file_skipped_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-existent file path is silently skipped; exits 0."""
        rc = cts.main(["/tmp/_nonexistent_tag_symmetry_test_file.md"])
        assert rc == 0

    def test_timeout_flag_accepted(self, tmp_path: Path) -> None:
        """Verify command-line option behavior.

        The ``--timeout`` flag is accepted and does not affect exit code.
        """
        f = tmp_path / "clean.md"
        f.write_text("<workflow>\nok\n</workflow>\n", encoding="utf-8")
        rc = cts.main([str(f), "--timeout", "5"])
        assert rc == 0


class TestMainSubcheckSelection:
    """Covers ``--check`` selecting one subcheck at a time on an all-modes-violating file."""

    @staticmethod
    def _all_modes_file(tmp_path: Path) -> Path:
        """Write a file that violates empty-block, unbalanced, and escaped-tag at once."""
        f = tmp_path / "all.md"
        f.write_text(
            "<objective></objective>\n<workflow>\nProse with \\<notes> escaped.\n",
            encoding="utf-8",
        )
        return f

    def test_no_arg_default_runs_every_subcheck(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Bare invocation reports all three violations and exits 1."""
        rc = cts.main([str(self._all_modes_file(tmp_path))])
        out = capsys.readouterr().out
        assert rc == 1
        assert "empty block" in out
        assert "unbalanced" in out
        assert "escaped structural tag" in out

    @pytest.mark.parametrize(
        ("mode", "expected", "excluded"),
        [
            pytest.param("empty-block", "empty block", ("unbalanced", "escaped structural tag"), id="empty-block"),
            pytest.param("unbalanced", "unbalanced", ("empty block", "escaped structural tag"), id="unbalanced"),
            pytest.param("escaped-tag", "escaped structural tag", ("empty block", "unbalanced"), id="escaped-tag"),
        ],
    )
    def test_single_subcheck_reports_only_its_own_findings(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        mode: str,
        expected: str,
        excluded: tuple[str, ...],
    ) -> None:
        """Selecting one subcheck reports that mode's findings only, still exiting 1."""
        rc = cts.main([str(self._all_modes_file(tmp_path)), "--check", mode])
        out = capsys.readouterr().out
        assert rc == 1
        assert expected in out
        assert not any(other in out for other in excluded)

    def test_subcheck_clean_for_unrelated_violation_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file with only an empty block passes the escaped-tag subcheck."""
        f = tmp_path / "empty_only.md"
        f.write_text("<constants></constants>\n", encoding="utf-8")
        rc = cts.main([str(f), "--check", "escaped-tag"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "[escaped-tag]" in out

    def test_unknown_subcheck_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unknown ``--check`` mode exits 2 with the token named on stderr."""
        rc = cts.main([str(self._all_modes_file(tmp_path)), "--check", "bogus"])
        assert rc == 2
        assert "bogus" in capsys.readouterr().err

    def test_read_error_survives_subcheck_narrowing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unreadable file is reported under any ``--check`` mode, never filtered away.

        The denial is injected rather than produced by ``chmod(0o000)``: Windows maps chmod onto the read-only attribute
        only, so the file stayed readable and the assertion tested the OS instead of the filter. The subject here is
        that a READ_ERROR finding survives ``--check`` narrowing, which an injected OSError exercises identically on
        every platform.
        """
        unreadable = tmp_path / "locked.md"
        unreadable.write_text("<role>\nok\n</role>\n", encoding="utf-8")
        real_read_text = Path.read_text

        def _deny(self: Path, *args: object, **kwargs: object) -> str:
            """Reject reads of the designated unreadable fixture path."""
            if self == unreadable:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", _deny)

        rc = cts.main([str(unreadable), "--check", "empty-block"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "cannot read" in out

    def test_oversized_file_read_error_survives_subcheck_narrowing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The size-guard READ_ERROR is never filtered away by a narrowed ``--check`` mode."""
        monkeypatch.setattr(cts, "_MAX_FILE_SIZE", 8)
        f = tmp_path / "huge.md"
        f.write_text("<constants></constants>\n", encoding="utf-8")

        rc = cts.main([str(f), "--check", "empty-block"])

        out = capsys.readouterr().out
        assert rc == 1
        assert "exceeds" in out

"""Tests for check_tag_symmetry bin script.

Covers empty-block detection, unbalanced tag detection, escaped-tag detection, the per-subcheck ``--check`` selector,
clean files, and CLI integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import check_tag_symmetry as cts


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

    def test_clean_file_returns_empty(self, tmp_path: Path) -> None:
        """File with properly balanced non-empty tags returns no violations."""
        f = tmp_path / "ok.md"
        f.write_text("<objective>\ncontent\n</objective>\n", encoding="utf-8")
        assert cts.check_file(f) == []

    def test_empty_block_detected(self, tmp_path: Path) -> None:
        """Empty structural block returns one violation."""
        f = tmp_path / "bad.md"
        f.write_text("<objective></objective>\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert len(violations) == 1
        assert "empty block <objective></objective>" in violations[0]

    def test_whitespace_only_block_detected(self, tmp_path: Path) -> None:
        """Block with only whitespace between tags is flagged as empty."""
        f = tmp_path / "ws.md"
        f.write_text("<notes>   </notes>\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert any("empty block" in v and "<notes>" in v for v in violations)

    def test_unbalanced_open_without_close(self, tmp_path: Path) -> None:
        """Tag opened but never closed is flagged as unbalanced."""
        f = tmp_path / "unclosed.md"
        f.write_text("<workflow>\ncontent\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert any("unbalanced" in v and "<workflow>" in v for v in violations)

    def test_unbalanced_close_without_open(self, tmp_path: Path) -> None:
        """Close tag without matching open tag is flagged as unbalanced."""
        f = tmp_path / "unopened.md"
        f.write_text("content\n</inputs>\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert any("unbalanced" in v and "<inputs>" in v for v in violations)

    def test_unreadable_file_returns_error(self, tmp_path: Path) -> None:
        """Non-existent path returns cannot-read violation instead of raising."""
        fake = tmp_path / "ghost.md"
        result = cts.check_file(fake)
        assert len(result) == 1
        assert result[0].kind is cts.FindingKind.READ_ERROR
        assert "cannot read" in result[0].message

    def test_multiple_tags_each_violation_reported(self, tmp_path: Path) -> None:
        """File with two empty blocks returns two violations."""
        f = tmp_path / "multi.md"
        f.write_text("<objective></objective>\n<notes></notes>\n", encoding="utf-8")
        violations = cts.check_file(f)
        assert len(violations) == 2

    def test_non_structural_tag_ignored(self, tmp_path: Path) -> None:
        """Tags not in the structural list are not checked."""
        f = tmp_path / "other.md"
        f.write_text("<example></example>\n<code></code>\n", encoding="utf-8")
        assert cts.check_file(f) == []

    def test_html_comment_with_tag_names_not_flagged(self, tmp_path: Path) -> None:
        """Structural tag names mentioned inside HTML comments don't inflate open count."""
        f = tmp_path / "comment.md"
        f.write_text(
            "<!-- Tag convention: <role>, <workflow>, <notes> are structural. -->\n"
            "<role>\ncontent\n</role>\n"
            "<workflow>\ncontent\n</workflow>\n"
            "<notes>\ncontent\n</notes>\n",
            encoding="utf-8",
        )
        assert cts.check_file(f) == []

    def test_code_fence_content_not_flagged_as_empty(self, tmp_path: Path) -> None:
        """Block containing only a code fence is not flagged as empty."""
        f = tmp_path / "fence.md"
        f.write_text(
            "<constants>\n\n```yaml\nKEY: value\n```\n\n</constants>\n",
            encoding="utf-8",
        )
        assert cts.check_file(f) == []

    def test_truly_empty_block_still_flagged(self, tmp_path: Path) -> None:
        """Block with only whitespace (no code fence) is still flagged as empty."""
        f = tmp_path / "empty.md"
        f.write_text("<constants>\n\n</constants>\n", encoding="utf-8")
        violations = _messages(cts.check_file(f))
        assert any("empty block" in v and "constants" in v for v in violations)

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

    def test_hyphenated_tag_name_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "hyphen.md"
        f.write_text("<routing-boundaries>\ncontent\n</routing-boundaries>\n", encoding="utf-8")
        assert cts.check_file(f) == []

    def test_underscore_placeholder_in_prose_not_flagged(self, tmp_path: Path) -> None:
        """Only whole-line tags are structural; `<a_b>` inside a sentence is a placeholder."""
        f = tmp_path / "prose.md"
        f.write_text("<role>\nWrite to <output_path> when done.\n</role>\n", encoding="utf-8")
        assert cts.check_file(f) == []

    def test_underscore_tag_inside_wide_fence_not_flagged(self, tmp_path: Path) -> None:
        """A four-backtick template fence hides its inner three-backtick block too."""
        f = tmp_path / "template.md"
        f.write_text(
            "<role>\ncontent\n</role>\n\n````\n**Run:**\n```\n<pytest_cmd>\n```\n````\n",
            encoding="utf-8",
        )
        assert cts.check_file(f) == []

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

    def test_escaped_tag_inside_backtick_not_flagged(self, tmp_path: Path) -> None:
        """Escaped structural tag inside inline backtick span is not flagged."""
        f = tmp_path / "backtick.md"
        f.write_text(
            "<role>\ncontent\n</role>\nUse `\\<notes>` to suppress navigation.\n",
            encoding="utf-8",
        )
        violations = _messages(cts.check_file(f))
        assert not any("escaped structural tag" in v for v in violations)

    def test_escaped_tag_inside_code_fence_not_flagged(self, tmp_path: Path) -> None:
        """Escaped structural tag inside fenced code block is not flagged."""
        f = tmp_path / "fence.md"
        f.write_text(
            "<role>\ncontent\n</role>\n```markdown\n\\<workflow>\nexample\n```\n",
            encoding="utf-8",
        )
        violations = _messages(cts.check_file(f))
        assert not any("escaped structural tag" in v for v in violations)

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

    def test_attributed_open_pairs_with_plain_close(self, tmp_path: Path) -> None:
        """An open tag carrying attributes still pairs with its bare closing tag.

        Collapsible sections in plugin READMEs are written `<details open>` on one line and closed by a bare
        `</details>` on its own. Matching the open form literally would count every such section as a missing open and
        bury a real defect under README noise.
        """
        f = tmp_path / "readme.md"
        f.write_text(
            "<details open>\n<summary>Title</summary>\n\nbody\n\n</details>\n",
            encoding="utf-8",
        )
        assert cts.check_file(f) == []

    def test_placeholder_only_seen_inline_is_never_discovered(self, tmp_path: Path) -> None:
        """A `<name>` that only ever appears mid-sentence is a placeholder, not a block.

        Skill prose is full of unpaired argument placeholders. Discovering names from any occurrence would report every
        one of them as unbalanced; requiring a standalone line is what separates a block from a placeholder.
        """
        f = tmp_path / "skill.md"
        f.write_text("Pass <output-path> to the script, then read <output-path>.\n", encoding="utf-8")
        assert cts.check_file(f) == []

    def test_code_span_quoting_an_html_comment_does_not_leak_later_tags(self, tmp_path: Path) -> None:
        """A line whose code span quotes an HTML comment keeps its later spans intact.

        Stripping comments before code spans deletes the comment out of the span and leaves its two backticks adjacent;
        every later span on the line then pairs one delimiter off, so quoted prose is treated as code and the tag it
        mentions escapes into the balance count. The documentation line that names a marker comment and a tag in the
        same sentence is the real case that produced a phantom unbalanced finding.
        """
        f = tmp_path / "doc.md"
        f.write_text(
            "The marker `<!-- policy-sibling: a.md, b.md -->` is required; "
            "see the curator `<workflow>` step for the follow-up.\n",
            encoding="utf-8",
        )
        assert cts.check_file(f) == []

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

    def test_read_error_is_not_selectable(self) -> None:
        """Read-error is always emitted, never nameable in ``--check``."""
        with pytest.raises(ValueError, match="read-error"):
            cts.parse_kinds("read-error")

    def test_unknown_token_raises(self) -> None:
        """An unrecognised mode name raises ValueError naming the token."""
        with pytest.raises(ValueError, match="bogus"):
            cts.parse_kinds("empty-block,bogus")


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

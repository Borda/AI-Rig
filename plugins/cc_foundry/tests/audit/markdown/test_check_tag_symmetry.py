"""Tests for check_tag_symmetry bin script.

Covers empty-block detection, unbalanced tag detection, escaped-tag detection, the per-subcheck ``--check`` selector,
clean files, and CLI integration. Every test writes its Markdown under ``tmp_path``; no test spawns a process.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest import mock

import check_tag_symmetry as cts
import pytest

_REAL_READ_TEXT = Path.read_text
_ALL_MODES_TEXT = "<objective></objective>\n<workflow>\nProse with \\<notes> escaped.\n"


@pytest.mark.timeout(3)
@pytest.mark.parametrize("fragment", ["<!--", "<notes x"])
def test_incomplete_markup_is_scanned_without_repeated_suffix_searches(tmp_path: Path, fragment: str) -> None:
    """A large run of unmatched comments or attributed tags cannot stall the structural scan."""
    path = tmp_path / "incomplete.md"
    path.write_text("<role>\ncontent\n</role>\n" + fragment * 20000, encoding="utf-8")
    assert cts.check_file(path) == []


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


def _deny_reads_of(blocked: Path) -> Callable[..., str]:
    """Build a ``Path.read_text`` stand-in that raises ``PermissionError`` for ``blocked`` only."""

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == blocked:
            raise PermissionError(13, "Permission denied")
        return _REAL_READ_TEXT(self, *args, **kwargs)  # type: ignore[arg-type]

    return read_text


@pytest.fixture
def md_file(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory that writes Markdown ``text`` to ``tmp_path / name`` and returns the path."""

    def write(text: str, name: str = "doc.md") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return write


class TestCheckFile:
    """Covers check_file() for individual file scenarios."""

    @pytest.mark.parametrize(
        ("text", "kind", "tag"),
        [
            pytest.param("<objective></objective>\n", "empty block", "<objective></objective>", id="empty-block"),
            pytest.param("<notes>   </notes>\n", "empty block", "<notes>", id="whitespace-only-block"),
            pytest.param("<constants>\n\n</constants>\n", "empty block", "constants", id="blank-lines-only-block"),
            pytest.param("<workflow>\ncontent\n", "unbalanced", "<workflow>", id="open-without-close"),
            pytest.param("content\n</inputs>\n", "unbalanced", "<inputs>", id="close-without-open"),
        ],
    )
    def test_defective_block_is_flagged(self, md_file: Callable[..., Path], text: str, kind: str, tag: str) -> None:
        """An empty or unbalanced structural block yields exactly one finding naming the defect and the tag.

        Covers a block with nothing between its tags, one holding only spaces, one holding only blank lines (no code
        fence to count as content), a tag opened and never closed, and a closing tag with no opening.
        """
        violations = _messages(cts.check_file(md_file(text)))
        assert len(violations) == 1
        assert kind in violations[0]
        assert tag in violations[0]

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
    def test_well_formed_text_returns_empty(self, md_file: Callable[..., Path], text: str) -> None:
        """Well-formed structural markup, and text only resembling it, returns no violations.

        Covers balanced non-empty blocks, tags outside the registry, tag names quoted in HTML comments or code spans,
        fence-only content, hyphenated names, underscore placeholders in prose or fences, attributed open tags and mid-
        sentence placeholders.
        """
        assert cts.check_file(md_file(text)) == []

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(
                "<role>\ncontent\n</role>\nUse `\\<notes>` to suppress navigation.\n", id="inline-backtick-span"
            ),
            pytest.param("<role>\ncontent\n</role>\n```markdown\n\\<workflow>\nexample\n```\n", id="fenced-code-block"),
        ],
    )
    def test_escaped_tag_in_code_is_not_flagged(self, md_file: Callable[..., Path], text: str) -> None:
        """A backslash-escaped structural tag inside an inline backtick span or a fenced block is not flagged.

        Documentation often shows an escaped tag as an example; only escapes in running prose are a defect.
        """
        violations = _messages(cts.check_file(md_file(text)))
        assert not any("escaped structural tag" in v for v in violations)

    def test_unreadable_file_returns_error(self, tmp_path: Path) -> None:
        """Non-existent path returns cannot-read violation instead of raising.

        The checker runs over a changed-file list from pre-commit, where a path can disappear between listing and
        reading.
        """
        result = cts.check_file(tmp_path / "ghost.md")
        assert len(result) == 1
        assert result[0].kind is cts.FindingKind.READ_ERROR
        assert "cannot read" in result[0].message

    def test_oversized_file_returns_read_error_without_being_read(
        self, md_file: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file over the size guard is reported as a READ_ERROR, never loaded into memory.

        The guard is exercised by lowering ``_MAX_FILE_SIZE`` rather than writing a real 10 MB fixture — the size
        comparison is the behavior under test, not the literal threshold.
        """
        monkeypatch.setattr(cts, "_MAX_FILE_SIZE", 8)
        result = cts.check_file(md_file("<objective></objective>\n"))
        assert len(result) == 1
        assert result[0].kind is cts.FindingKind.READ_ERROR
        assert "exceeds" in result[0].message

    def test_multiple_tags_each_violation_reported(self, md_file: Callable[..., Path]) -> None:
        """File with two empty blocks returns two violations.

        Findings are per block, not per file, so one defect cannot hide another.
        """
        assert len(cts.check_file(md_file("<objective></objective>\n<notes></notes>\n"))) == 2

    def test_underscore_tag_name_flagged_with_hyphen_suggestion(self, md_file: Callable[..., Path]) -> None:
        """A block tag whose name carries an underscore is not a CommonMark tag.

        The finding names the offending tag and proposes the hyphenated spelling to use instead.
        """
        path = md_file("<routing_boundaries>\ncontent\n</routing_boundaries>\n")
        violations = _messages(cts.check_file(path))
        assert any("underscore in structural tag <routing_boundaries>" in v for v in violations)
        assert any("<routing-boundaries>" in v for v in violations)

    def test_underscore_check_is_not_limited_to_the_registry(self, md_file: Callable[..., Path]) -> None:
        """A name nobody registered still breaks the same way, so it is still flagged.

        Registration only governs the empty-block check; the underscore defect depends on the spelling alone.
        """
        path = md_file("<never_registered>\ncontent\n</never_registered>\n")
        assert cts.FindingKind.UNDERSCORE_TAG in [v.kind for v in cts.check_file(path)]

    def test_escaped_structural_tag_flagged_as_low(self, md_file: Callable[..., Path]) -> None:
        """Backslash-escaped structural tag in prose is flagged with [low] severity.

        An escape in running prose suppresses block navigation, which is why it is reported, but it breaks nothing else,
        which is why it is low severity.
        """
        path = md_file("<role>\ncontent\n</role>\nProse mentioning \\<antipatterns-to-flag> should be flagged.\n")
        violations = _messages(cts.check_file(path))
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
    def test_every_structural_tag_is_checked_when_empty(self, md_file: Callable[..., Path], tag: str) -> None:
        """Every structural tag name triggers an empty-block finding when empty.

        The registry is a fixed list; a name dropped from it would silently stop being checked.
        """
        violations = _messages(cts.check_file(md_file(f"<{tag}></{tag}>\n")))
        assert any("empty block" in v for v in violations)

    def test_every_finding_carries_its_kind(self, md_file: Callable[..., Path]) -> None:
        """A file violating all three modes yields one finding of each kind.

        The kind is what ``--check`` filters on, so every finding must carry the one matching its defect.
        """
        kinds = {finding.kind for finding in cts.check_file(md_file(_ALL_MODES_TEXT))}
        assert kinds == {cts.FindingKind.EMPTY_BLOCK, cts.FindingKind.UNBALANCED, cts.FindingKind.ESCAPED_TAG}


class TestDynamicBalance:
    """Covers balance checking of tag names discovered in the file rather than registered."""

    def test_unregistered_block_duplicated_by_prose_fusion_is_flagged(self, md_file: Callable[..., Path]) -> None:
        """A block whose opening tag also survives inside a prose paragraph is unbalanced.

        This is the shape a paragraph-fusion repair leaves behind: the original tag had been swallowed into the intro
        paragraph, and separating it out re-emitted the tag without removing the swallowed copy, so two opens face a
        single close. The name belongs to no registry, which is precisely why the registry-driven check reported the
        file clean.
        """
        path = md_file(
            "Intro sentence naming the block. <split-strategies>\n"
            "\n"
            "<split-strategies>\n"
            "\n"
            "content\n"
            "\n"
            "</split-strategies>\n"
        )
        violations = _messages(cts.check_file(path))
        assert len(violations) == 1
        assert "unbalanced <split-strategies> — 2 open, 1 close" in violations[0]

    def test_legacy_underscore_block_is_both_renamed_and_balance_checked(self, md_file: Callable[..., Path]) -> None:
        """A legacy underscore block reports the rename and its balance defect together.

        Underscore names are on the way out, but one that is still in the tree can still be mis-paired. Admitting them
        to discovery means the balance defect is reported in the same run as the rename advice, instead of waiting for
        the rename to land first.
        """
        findings = cts.check_file(md_file("<legacy_block>\n\ncontent\n\n<legacy_block>\n\n</legacy_block>\n"))
        assert {finding.kind for finding in findings} == {cts.FindingKind.UNDERSCORE_TAG, cts.FindingKind.UNBALANCED}
        assert any("unbalanced <legacy_block> — 2 open, 1 close" in m for m in _messages(findings))


class TestParseKinds:
    """Covers parse_kinds() selector parsing."""

    def test_all_selectable_kinds_parse(self) -> None:
        """The default spec resolves to every selectable kind.

        A spec naming each selectable kind once must round-trip to exactly the selectable set.
        """
        spec = ",".join(k.value for k in cts.SELECTABLE_KINDS)
        assert cts.parse_kinds(spec) == set(cts.SELECTABLE_KINDS)

    def test_whitespace_and_case_tolerated(self) -> None:
        """Tokens are trimmed and lower-cased before lookup.

        A hand-typed ``--check`` value with stray spaces or capitals still selects the intended subchecks.
        """
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
        """Accept an empty file list and report a passing result.

        pre-commit may invoke the hook with no matching files; that is a pass, not an error.
        """
        rc = cts.main([])
        assert rc == 0
        assert "no files provided" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("text", "expected", "marker"),
        [
            pytest.param("<objective>\ncontent\n</objective>\n", 0, "✓", id="clean-file"),
            pytest.param("<constants></constants>\n", 1, "! C14a:", id="empty-block-uses-stable-id"),
        ],
    )
    def test_exit_code_and_marker_mirror_findings(
        self, md_file: Callable[..., Path], capsys: pytest.CaptureFixture[str], text: str, expected: int, marker: str
    ) -> None:
        """A clean file exits 0 with a pass line; an empty block exits 1 with the stable ``C14a`` diagnostic id.

        Users and tooling key on the exit code and on the diagnostic identifier, so both must stay stable.
        """
        rc = cts.main([str(md_file(text))])
        assert rc == expected
        assert marker in capsys.readouterr().out

    def test_nonexistent_file_skipped_exits_zero(self, tmp_path: Path) -> None:
        """Non-existent file path is silently skipped; exits 0.

        A file deleted by the same commit is still in the hook's argument list and must not fail the run.
        """
        assert cts.main([str(tmp_path / "gone.md")]) == 0

    def test_timeout_flag_accepted(self, md_file: Callable[..., Path]) -> None:
        """The ``--timeout`` flag is accepted and does not affect exit code.

        Every audit script shares that flag; a clean file with it set must still exit 0.
        """
        assert cts.main([str(md_file("<workflow>\nok\n</workflow>\n")), "--timeout", "5"]) == 0


class TestMainSubcheckSelection:
    """Covers ``--check`` selecting one subcheck at a time on an all-modes-violating file."""

    @pytest.fixture
    def all_modes_file(self, md_file: Callable[..., Path]) -> Path:
        """File that violates empty-block, unbalanced, and escaped-tag at once."""
        return md_file(_ALL_MODES_TEXT, name="all.md")

    def test_no_arg_default_runs_every_subcheck(self, all_modes_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Bare invocation reports all three violations and exits 1.

        Without ``--check`` every subcheck runs, so each defect class must appear in the output.
        """
        rc = cts.main([str(all_modes_file)])
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
        all_modes_file: Path,
        capsys: pytest.CaptureFixture[str],
        mode: str,
        expected: str,
        excluded: tuple[str, ...],
    ) -> None:
        """Selecting one subcheck reports that mode's findings only, still exiting 1.

        The file violates all three modes, so any leakage from an unselected mode would show in the output.
        """
        rc = cts.main([str(all_modes_file), "--check", mode])
        out = capsys.readouterr().out
        assert rc == 1
        assert expected in out
        assert not any(other in out for other in excluded)

    def test_subcheck_clean_for_unrelated_violation_exits_zero(
        self, md_file: Callable[..., Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file with only an empty block passes the escaped-tag subcheck.

        The run still announces the ``[escaped-tag]`` subcheck it performed, so a silent skip is distinguishable.
        """
        rc = cts.main([str(md_file("<constants></constants>\n")), "--check", "escaped-tag"])
        assert rc == 0
        assert "[escaped-tag]" in capsys.readouterr().out

    def test_unknown_subcheck_exits_two(self, all_modes_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unknown ``--check`` mode exits 2 with the token named on stderr."""
        assert cts.main([str(all_modes_file), "--check", "bogus"]) == 2
        assert "bogus" in capsys.readouterr().err

    def test_read_error_survives_subcheck_narrowing(
        self, md_file: Callable[..., Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unreadable file is reported under any ``--check`` mode, never filtered away.

        The denial is injected rather than produced by ``chmod(0o000)``: Windows maps chmod onto the read-only attribute
        only, so the file stayed readable and the assertion tested the OS instead of the filter. The subject here is
        that a READ_ERROR finding survives ``--check`` narrowing, which an injected OSError exercises identically on
        every platform.
        """
        unreadable = md_file("<role>\nok\n</role>\n", name="locked.md")

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=_deny_reads_of(unreadable)):
            rc = cts.main([str(unreadable), "--check", "empty-block"])

        assert rc == 1
        assert "cannot read" in capsys.readouterr().out

    def test_oversized_file_read_error_survives_subcheck_narrowing(
        self, md_file: Callable[..., Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size-guard READ_ERROR is never filtered away by a narrowed ``--check`` mode.

        A file over the size cap is reported even when only the empty-block subcheck is selected.
        """
        monkeypatch.setattr(cts, "_MAX_FILE_SIZE", 8)
        rc = cts.main([str(md_file("<constants></constants>\n", name="huge.md")), "--check", "empty-block"])
        assert rc == 1
        assert "exceeds" in capsys.readouterr().out

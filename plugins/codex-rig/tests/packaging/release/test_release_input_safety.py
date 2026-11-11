"""Bound contributor-controlled Markdown before release validation scans it."""

import sys
from pathlib import Path

import pytest
from _modules import load_module

SHARED = Path(__file__).resolve().parents[3] / "shared"
sys.path.insert(0, str(SHARED))
HELPER = load_module("release_input_safety", SHARED / "release_evidence.py")


@pytest.mark.parametrize(
    "scan",
    [pytest.param("heading", id="heading-tail"), pytest.param("credit", id="nested-link-label")],
)
def test_oversized_markdown_line_remains_nonfatal(scan: str) -> None:
    """Long headings retain section semantics and cannot manufacture contributor credit."""
    text = "## " + " " * 100_000 + "!"

    result = HELPER.release_sections(text) if scan == "heading" else HELPER._visible_credit_name(text, "Person")
    assert result == ([("!", 0, len(text), len(text))] if scan == "heading" else False)


def test_atx_heading_removes_only_separated_closing_hashes() -> None:
    """Linear heading parsing preserves literal trailing hashes and optional closing markers."""
    assert [row[0] for row in HELPER.release_sections("## Version#\n\n## Version ##  \n")] == ["Version#", "Version"]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("[" * 100_000, id="unmatched-label-openers"),
        pytest.param("[" * 50_000 + "]" * 50_000, id="nested-labels"),
        pytest.param("*a_" * 40_000, id="emphasis-runs"),
    ],
)
def test_oversized_credit_syntax_is_nonmatching(text: str) -> None:
    """Malformed and deeply nested markup cannot abort validation or count as credit."""
    assert HELPER._visible_credit_name(text, "Person") is False


def test_reference_definition_survives_a_long_following_paragraph() -> None:
    """Bounding a continuation candidate preserves an already complete reference definition."""
    context = "[person]: https://example.org\n" + ("ordinary text\n" * 500)
    assert HELPER._visible_credit_name("[Person][person]", "Person", context=context)


def test_atx_heading_without_title_is_not_added() -> None:
    """A heading prefix with only its required separator retains its previous interpretation."""
    assert HELPER.release_sections("## \n") == []


class _CountedName(str):
    """Measure author normalization while preserving ordinary string splitting."""

    calls = 0

    def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
        """Count one normalization call and return the standard string result."""
        self.calls += 1
        return super().split(sep, maxsplit)


@pytest.mark.parametrize(
    ("credit_text", "expected"),
    [
        pytest.param("Other One\nOther Two\nOther Three", False, id="author-absent"),
        pytest.param("Other One\nOther Two\nOther Three\nVerified Person", True, id="author-after-nonmatches"),
    ],
)
def test_author_name_normalization_is_independent_of_credit_line_count(credit_text: str, expected: bool) -> None:
    """One author normalization preserves both absent and late matching credit results."""
    name = _CountedName("Verified Person")

    assert HELPER._visible_credit_name(credit_text, name) is expected
    assert name.calls == 1


def test_long_fenced_line_preserves_release_section() -> None:
    """Fenced log output never prevents subsequent release headings from being found."""
    text = "## Version\n```\n" + "x" * 5000 + "\n```\n## Older\n"

    assert [section[0] for section in HELPER.release_sections(text)] == ["Version", "Older"]
    assert not HELPER._visible_credit_name(text, "Person")


def test_long_contributor_line_is_nonmatching() -> None:
    """An overlong contributor paragraph is skipped without hiding later valid credit."""
    line = "Thanks to " + ", ".join(f"@user{i}" for i in range(500))
    assert len(line) == 4898
    assert not HELPER._visible_credit_name(line, "Person")
    assert HELPER._visible_credit_name(line + "\n- Person", "Person")


def test_long_incomplete_reference_is_nonmatching() -> None:
    """An incomplete multi-line link destination is bounded without aborting credit validation."""
    context = "[person]:\n<" + "x" * 3000 + "\n" + "y" * 3000

    assert not HELPER._visible_credit_name("- [Person][person]", "Person", context=context)


@pytest.mark.parametrize("line", ["- Jane Doe  (@jane)", "- Jane Doe  (@jane) - fixed docs"])
def test_credit_name_with_repeated_spaces_before_handle(line: str) -> None:
    """A whitespace run before an annotation leaves the visible contributor name intact."""
    assert HELPER._visible_credit_name(line, "Jane Doe")

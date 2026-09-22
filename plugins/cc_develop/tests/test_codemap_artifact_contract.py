"""Parity between the review->resolve cache contract as *documented* and as *implemented*.

``skills/_shared/codemap-context.md`` documents the artifact shape and freshness rule that
``cc_oss/bin/codemap_cache.py`` implements. The doc previously described a two-field rule (``git_sha`` + ``scanned_at``)
after the implementation had already gained the fail-closed ``index_stamp`` field, so a reader following the doc would
have written artifacts the reader rejects. These tests pin doc and implementation together.

The implementation lives in a sibling plugin. That is fine to read from a **test** (tests are not shipped runtime), but
a standalone cc_develop checkout may not have it, so the cross-plugin assertions skip rather than fail when it is
absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DEVELOP = Path(__file__).resolve().parent.parent
_DOC = _DEVELOP / "skills" / "_shared" / "codemap-context.md"
_IMPL = _DEVELOP.parent / "cc_oss" / "bin" / "codemap_cache.py"
_HAS_OSS_IMPLEMENTATION = _IMPL.is_file()


@pytest.fixture(name="doc_text", scope="module")
def _doc_text() -> str:
    """Return the develop-side codemap context document.

    >>> text = getfixture("doc_text")
    >>> "index_stamp" in text
    True
    """
    return _DOC.read_text(encoding="utf-8")


@pytest.fixture(name="impl_text", scope="module")
def _impl_text() -> str:
    """Return the installed oss-side cache implementation.

    >>> text = getfixture("impl_text")
    >>> "index_stamp" in text
    True
    """
    return _IMPL.read_text(encoding="utf-8")


def test_artifact_shape_documents_index_stamp(doc_text: str) -> None:
    """The prefix block must show the stamp; an artifact written without one is rejected."""
    shape = doc_text.split('{"module": "pkg.mod"', 1)[1].split("```", 1)[0]
    assert "index_stamp" in shape
    assert "<canonical-path>:<size>:<mtime_ns>" in shape


def test_freshness_rule_is_fail_closed(doc_text: str) -> None:
    """The doc must state all three conditions, not just git_sha + scanned_at."""
    rule = doc_text.split("**Freshness rule**", 1)[1].split("\n\n", 1)[0]
    for field in ("git_sha", "scanned_at", "index_stamp"):
        assert field in rule, f"freshness rule omits {field}"
    assert "fail" in rule.lower()


@pytest.mark.skipif(not _HAS_OSS_IMPLEMENTATION, reason="requires cc_oss cache implementation for parity check")
def test_documented_verdict_reasons_all_exist(doc_text: str, impl_text: str) -> None:
    """Every reason string the doc advertises must be one the implementation can return."""
    rule = doc_text.split("**Freshness rule**", 1)[1].split("\n\n", 1)[0]
    documented = set(re.findall(r"`(fresh|[a-z_]+_mismatch|index_rebuilt)`", rule))
    implemented = set(re.findall(r'return (?:False|True), "([a-z_]+)"', impl_text))
    assert documented, "no verdict reasons documented"
    assert documented <= implemented, f"documented but unreachable: {sorted(documented - implemented)}"


@pytest.mark.skipif(not _HAS_OSS_IMPLEMENTATION, reason="requires cc_oss cache implementation for parity check")
def test_no_reason_left_undocumented(doc_text: str, impl_text: str) -> None:
    """A consumer branching on verdicts needs the full set, not a subset."""
    rule = doc_text.split("**Freshness rule**", 1)[1].split("\n\n", 1)[0]
    implemented = set(re.findall(r'return (?:False|True), "([a-z_]+)"', impl_text))
    missing = {r for r in implemented if f"`{r}`" not in rule}
    assert not missing, f"implementation returns undocumented verdicts: {sorted(missing)}"

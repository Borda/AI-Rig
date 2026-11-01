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

_DEVELOP = Path(__file__).resolve().parent.parent.parent
_DOC = _DEVELOP / "skills" / "_shared" / "codemap-context.md"
_IMPL = _DEVELOP.parent / "cc_oss" / "bin" / "codemap_cache.py"
_requires_oss_implementation = pytest.mark.skipif(
    not _IMPL.is_file(), reason="requires cc_oss cache implementation for parity check"
)

# Reasons reach the verdict from three return shapes: a bare string from a per-check helper,
# that string leading a tuple, and the final ``True, "fresh"``. The variable-carrying
# ``return False, reason`` in the verdict itself is deliberately not matched.
_REASON_RE = re.compile(r'return (?:(?:False|True), )?"([a-z_]+)"')


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


@pytest.fixture(name="freshness_rule", scope="module")
def _freshness_rule(doc_text: str) -> str:
    """Return the freshness-rule paragraph of the document (from its bold label to the next blank line)."""
    return doc_text.split("**Freshness rule**", 1)[1].split("\n\n", 1)[0]


def test_artifact_shape_documents_index_stamp(doc_text: str) -> None:
    """The documented artifact prefix shows the stamp field and its format.

    An artifact written without the stamp is rejected by the reader, so a doc that omits it teaches a broken writer.
    """
    shape = doc_text.split('{"module": "pkg.mod"', 1)[1].split("```", 1)[0]
    assert "index_stamp" in shape
    assert "<canonical-path>:<size>:<mtime_ns>" in shape


@pytest.mark.parametrize("field", ["git_sha", "scanned_at", "index_stamp"])
def test_freshness_rule_names_every_condition(freshness_rule: str, field: str) -> None:
    """The freshness rule states all three conditions, not just ``git_sha`` + ``scanned_at``.

    The implementation became three-field first; a two-field doc let writers produce artifacts the reader rejects.
    """
    assert field in freshness_rule


def test_freshness_rule_is_fail_closed(freshness_rule: str) -> None:
    """The freshness rule says an unverifiable artifact fails instead of being trusted."""
    assert "fail" in freshness_rule.lower()


@_requires_oss_implementation
def test_documented_verdict_reasons_all_exist(freshness_rule: str, impl_text: str) -> None:
    """Every reason string the doc advertises is one the implementation can return.

    A documented verdict the code never returns would send consumers waiting on a branch that cannot fire.
    """
    documented = set(re.findall(r"`(fresh|[a-z_]+_mismatch|index_rebuilt)`", freshness_rule))
    implemented = set(_REASON_RE.findall(impl_text))
    assert documented, "no verdict reasons documented"
    assert documented <= implemented, f"documented but unreachable: {sorted(documented - implemented)}"


@_requires_oss_implementation
def test_no_reason_left_undocumented(freshness_rule: str, impl_text: str) -> None:
    """Every reason the implementation can return is documented in the freshness rule.

    A consumer branching on verdicts needs the full set, not a subset.
    """
    implemented = set(_REASON_RE.findall(impl_text))
    missing = {r for r in implemented if f"`{r}`" not in freshness_rule}
    assert not missing, f"implementation returns undocumented verdicts: {sorted(missing)}"

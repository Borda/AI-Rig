"""Shared paid-stage lifecycle and approval-token contracts."""

from __future__ import annotations

import pytest

from benchmarks._bench_common.paid_lifecycle import paid_approval_matches, paid_approval_token

SCOPE_SHA256 = "0123456789abcdef" * 4


def test_paid_approval_token_shortens_presentation_without_changing_scope_identity() -> None:
    """Human confirmation uses 16 hex characters while stored scope stays complete.

    An operator types the short token; the 64-character scope hash it abbreviates is neither truncated nor altered, so
    the stored scope identity remains the full digest.
    """
    assert paid_approval_token(SCOPE_SHA256) == "0123456789abcdef"
    assert len(SCOPE_SHA256) == 64


@pytest.mark.parametrize(
    ("received", "expected"),
    [
        pytest.param("0123456789abcdef", True, id="canonical-prefix"),
        pytest.param("0123456789abcdef0", True, id="longer-prefix"),
        pytest.param(SCOPE_SHA256, True, id="legacy-full-hash"),
        pytest.param("0123456789abcde", False, id="too-short"),
        pytest.param(SCOPE_SHA256 + "0", False, id="too-long"),
        pytest.param("0123456789abcdeg", False, id="non-hex"),
        pytest.param("0123456789ABCDEf", False, id="uppercase"),
        pytest.param("1123456789abcdef", False, id="different-scope"),
        pytest.param(None, False, id="missing"),
    ],
)
def test_paid_approval_matching_is_prefix_bounded_and_fail_closed(received: str | None, expected: bool) -> None:
    """Only lowercase matching prefixes of at least 64 bits authorize the current scope.

    Every malformed, short, over-long, differently-scoped or absent token must fail closed, because a false positive
    authorizes a run that spends money.
    """
    assert paid_approval_matches(received, SCOPE_SHA256) is expected


def test_paid_approval_token_rejects_noncanonical_scope_hash() -> None:
    """Presentation cannot conceal malformed or incomplete scope identity.

    A scope hash that is not 64 lowercase hexadecimal characters would yield a token for an identity that is not a real
    digest, so the abbreviation is refused instead of being shown to the operator.
    """
    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        paid_approval_token("not-a-scope")

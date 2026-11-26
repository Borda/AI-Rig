"""Contract tests for the unknown-impact cause in review_impact_tier.py and the review blocks that write it.

An "impact unknown" line with no reason cannot tell a disabled codemap from a stale index or an unmapped file. Every
unknown-impact line now starts its reason with one fixed cause phrase that maps back to a slug; a stale or root-
mismatched index can no longer yield ``LIGHT``; and the review's codemap block names a missing binary instead of keeping
the generic default. Unknown impact stays ``FULL``.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import review_impact_tier as rit

_MODES = Path(__file__).resolve().parents[2] / "skills/review/modes"
_LIGHT_PAYLOAD = {"changed_modules": [{"path": "src/pkg/viz/draw.py", "changed_symbols": ["pkg.viz.draw::_box"]}]}


def _no_callers(_qname: str) -> dict:
    """Answer fn-blast with an empty caller set, so the payload alone would be LIGHT."""
    return {"blast_radius": []}


def _lines(name: str) -> list[str]:
    """Read one review mode file as lines."""
    return (_MODES / name).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("cause", list(rit.ImpactCause))
def test_every_cause_round_trips_through_its_reason_line(cause: rit.ImpactCause) -> None:
    """The printed line for each cause maps back to that cause, with or without a detail after the phrase.

    A phrase that another phrase extends must still resolve to itself, never to the shorter one.
    """
    line = f"FULL · {rit.unknown(cause, 'detail-x')}"
    assert (rit.cause_of(line), rit.cause_of(f"FULL · {rit.unknown(cause)}")) == (cause, cause)


@pytest.mark.parametrize(
    ("index", "cause"),
    [
        pytest.param({"stale": True}, rit.ImpactCause.INDEX_STALE, id="stale"),
        pytest.param({"stale": False, "root_mismatch": True}, rit.ImpactCause.INDEX_ROOT_MISMATCH, id="root-mismatch"),
    ],
)
def test_untrusted_index_never_yields_light(index: dict, cause: rit.ImpactCause) -> None:
    """A stale or root-mismatched index makes a would-be LIGHT change FULL, with that cause named.

    The index no longer maps changed lines to the right symbols, so a trimmed lineup would rest on a wrong map.
    """
    tier, reason = rit.classify({**_LIGHT_PAYLOAD, "index": index}, _no_callers)
    assert (tier, rit.cause_of(reason)) == ("FULL", cause)


def test_error_answer_is_a_failed_query() -> None:
    """A codemap error object on stdout is a failed query, never an empty change set.

    The error code follows the phrase, so the report shows which failure it was.
    """
    tier, reason = rit.classify({"error": "index_busy"}, _no_callers)
    assert (tier, reason) == ("FULL", "impact unknown: diff-impact query failed index_busy")


@pytest.mark.parametrize(
    ("argv", "stdin", "expected"),
    [
        pytest.param([], "", "FULL · impact unknown: diff-impact query failed (no output)", id="empty-stdin"),
        pytest.param(["--unknown", "codemap-not-installed"], "", "FULL · impact unknown: codemap not installed",
                     id="caller-detected-cause"),
    ],
)  # fmt: skip
def test_main_names_the_cause(
    argv: list[str], stdin: str, expected: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty input and a caller-detected cause each print one FULL line naming the cause, exit 0.

    codemap-py writes its errors to stderr, which the review block discards, so empty stdin means the query failed.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert (rit.main(argv), capsys.readouterr().out.strip()) == (0, expected)


def test_review_default_sentinel_line_is_a_named_cause() -> None:
    """The pre-codemap default the scope block writes already names the ``codemap-unavailable`` cause.

    The line itself is unchanged, so reports and blocks quoting it keep working.
    """
    echo = [ln for ln in _lines("scope-context.md") if "oss-review-impact-" in ln and ln.startswith("echo ")]
    line = echo[0].split('"')[1]
    assert (len(echo), rit.cause_of(line)) == (1, rit.ImpactCause.CODEMAP_UNAVAILABLE)


def test_codemap_block_names_a_missing_binary() -> None:
    """When codemap is enabled but its binary is absent, the block writes the ``codemap-not-installed`` line.

    Before, that case kept the generic default and the report could not tell it from a disabled codemap.
    """
    lines = _lines("codemap-context.md")
    branch = lines.index('elif [ "$CODEMAP_ENABLED" = "true" ]; then')
    assert "--unknown codemap-not-installed" in lines[branch + 2]
    assert 'oss-review-impact-${CLEAN_ARGS}-${CSID}"' in lines[branch + 2]

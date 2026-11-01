"""Keep debugging challenges proportional without weakening diagnosis stops."""

from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[2] / "rules"


@pytest.mark.parametrize(
    ("relative", "fragment"),
    [
        pytest.param("debugging.md", "one bounded independent post-fix challenge", id="stub-one-challenge"),
        pytest.param("_full/debugging.md", "one independent post-fix challenge", id="full-one-challenge"),
        pytest.param("debugging.md", "ordinary diagnosis does not start that loop", id="stub-no-loop-for-ordinary-fix"),
        pytest.param(
            "_full/debugging.md",
            "A single challenge does not start an iterative review-and-fix loop",
            id="full-no-loop-for-single-challenge",
        ),
        pytest.param(
            "debugging.md", "Same failure signature twice in a row → stop immediately", id="stub-repeat-signature-stop"
        ),
        pytest.param("_full/debugging.md", "max 3 diagnosis-fix iterations", id="full-iteration-cap"),
        pytest.param("debugging.md", "Falsification before closing", id="stub-falsification"),
        pytest.param("_full/debugging.md", "### Falsification check", id="full-falsification-heading"),
    ],
)
def test_debugging_keeps_proportional_challenge_and_recurrence_stops(relative: str, fragment: str) -> None:
    """Ordinary fixes get one independent challenge, not a scored loop, and the diagnosis stops stay in force.

    The stub and the full rule must agree: a single post-fix challenge, the repeated-signature stop, the iteration cap
    and the falsification check all remain, so proportionality never weakens root-cause discipline.
    """
    assert fragment in (RULES / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "fragment"),
    [
        pytest.param("debugging.md", "stakes-gated, not routine", id="stub-no-stakes-gate"),
        pytest.param(
            "_full/debugging.md", "**Stakes gate — fires before every rule below.**", id="full-no-stakes-gate"
        ),
    ],
)
def test_debugging_drops_retired_stakes_gate(relative: str, fragment: str) -> None:
    """The retired stakes-gate wording no longer appears, so ordinary fixes do not skip their challenge.

    The old gate let a fix bypass the independent challenge entirely; reintroducing its wording would undo the change.
    """
    assert fragment not in (RULES / relative).read_text(encoding="utf-8")

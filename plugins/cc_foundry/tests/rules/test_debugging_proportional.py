"""Keep debugging challenges proportional without weakening diagnosis stops."""

from pathlib import Path

RULES = Path(__file__).resolve().parents[2] / "rules"


def test_debugging_uses_one_independent_challenge_and_retains_recurrence_stops() -> None:
    """Prevent ordinary fixes from starting a scored loop or skipping their challenge."""
    stub = (RULES / "debugging.md").read_text(encoding="utf-8")
    full = (RULES / "_full/debugging.md").read_text(encoding="utf-8")

    assert "one bounded independent post-fix challenge" in stub
    assert "one independent post-fix challenge" in full
    assert "ordinary diagnosis does not start that loop" in stub
    assert "A single challenge does not start an iterative review-and-fix loop" in full
    assert "Same failure signature twice in a row → stop immediately" in stub
    assert "max 3 diagnosis-fix iterations" in full
    assert "Falsification before closing" in stub
    assert "### Falsification check" in full
    assert "stakes-gated, not routine" not in stub
    assert "**Stakes gate — fires before every rule below.**" not in full

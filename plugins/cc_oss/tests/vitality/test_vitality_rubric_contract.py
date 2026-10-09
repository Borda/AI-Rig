"""Contract tests for the deterministic vitality scoring rules shared by the rubric files and oss:repo-warden.

Two scorers reading identical extractor output must report the same score and confidence. Each rule is restated in every
file a scorer instance reads, so each copy is pinned here; a dropped copy silently returns one group to judgment-based
placement.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import vitality_extract as vx

_PLUGIN = Path(__file__).resolve().parents[2]
_SHARED = _PLUGIN / "skills" / "_shared"
_FILES = {
    "canonical": _SHARED / "vitality-scoring.md",
    "group-a": _SHARED / "vitality-scoring-group-a.md",
    "group-b": _SHARED / "vitality-scoring-group-b.md",
    "group-c": _SHARED / "vitality-scoring-group-c.md",
    "repo-warden": _PLUGIN / "agents" / "repo-warden.md",
}


@pytest.mark.parametrize("name", list(_FILES))
def test_in_band_anchor_rule_present(name: str) -> None:
    """Every scorer-facing file fixes band-only scores: 🟢 10, 🔴/🟡 by anchor plus held 🟢 clauses.

    Without the rule, models placed identical facts at different numbers inside one band (observed 4.0 vs 5.5). 🟢 is
    a fixed 10: with an anchor of 7 plus clauses held, two-clause axes could never exceed 9.
    """
    # Act
    text = _FILES[name].read_text(encoding="utf-8")

    # Assert
    assert "🔴 1" in text
    assert "🟡 4" in text
    assert "🟢 10" in text
    assert "🟢 7" not in text


@pytest.mark.parametrize("name", list(_FILES))
def test_confidence_listed_degraders_only(name: str) -> None:
    """Every scorer-facing file limits confidence to listed degraders and routes other concerns to notes.

    Without the rule, one model deducted 0.1-0.2 for unlisted concerns while another applied none on the same data.
    """
    # Act
    text = _FILES[name].read_text(encoding="utf-8")

    # Assert
    assert "listed degraders only" in text.lower()
    assert "never into `conf`" in text or "never `conf`" in text


@pytest.mark.parametrize("marker", ["Confidence formula", "Band rule", "Copy rule"])
def test_shared_rule_is_byte_identical_in_every_file(marker: str) -> None:
    """Every scorer-facing file states each shared rule once, with the same text as the canonical rubric.

    Each file used to word the confidence rule its own way, and two of the wordings contradicted each other (a listed
    degrader replacing vs adding to -0.05 per indeterminate checkpoint), so a phrase-presence check passed while the
    models diverged.
    """
    # Arrange
    pattern = re.compile(rf"\*\*{marker}\*\*[^\n]*")

    # Act
    found = {name: pattern.findall(path.read_text(encoding="utf-8")) for name, path in _FILES.items()}

    # Assert
    assert all(len(lines) == 1 for lines in found.values()), found
    assert len({lines[0] for lines in found.values()}) == 1, found


def test_confidence_formula_replaces_rather_than_adds() -> None:
    """The canonical formula lets a listed degrader replace the per-checkpoint -0.05, never add to it."""
    # Act
    text = _FILES["canonical"].read_text(encoding="utf-8")

    # Assert
    assert "no applied listed degrader covers" in text
    assert "never both" in text


@pytest.mark.parametrize("name", ["group-c", "repo-warden"])
def test_axis3_fallback_has_one_definition(name: str) -> None:
    """Define the fallback bus factor once: distinct non-bot ``commits_50`` authors capped at 3.

    Three files had defined it three ways (last 100 commits; authors with ≥5% of commits; all authors capped at 3).
    """
    # Act
    text = _FILES[name].read_text(encoding="utf-8")

    # Assert
    assert "distinct non-bot authors in `commits_50`" in text
    assert "capped at 3" in text
    assert "≥5% of total commits" not in text


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(vx._TESTS_RE, id="checkpoint-2-tests"),
        pytest.param(vx._LINT_RE, id="checkpoint-3-lint"),
        pytest.param(vx._SAST_RE, id="checkpoint-4-security-scan"),
        pytest.param(vx._ABANDON_RE, id="axis-2-abandonment-override"),
        pytest.param(vx._BANNER_RE, id="axis-2-abandonment-banner"),
        pytest.param(vx._TITLE_BANNER_RE, id="axis-2-abandonment-title-banner"),
        pytest.param(vx._ABANDON_DESCRIPTION_RE, id="axis-2-abandonment-override-description"),
    ],
)
def test_group_a_rubric_states_the_extractor_pattern(pattern: object) -> None:
    """The Group A rubric quotes each extractor regex verbatim, so the documented rule cannot drift from the code.

    The CI test-step list once named only ``pytest``-style runners while repositories ran tests through ``tox -e``;
    rubric and extractor must change together.
    """
    # Act
    text = _FILES["group-a"].read_text(encoding="utf-8")

    # Assert
    assert f"`{pattern.pattern}`" in text  # type: ignore[attr-defined]


def test_health_score_never_folds_confidence_into_weight() -> None:
    """The vitality mode keeps Axis 3 at full weight as the assembler computes it; confidence is never folded in.

    An "effective weight × conf" instruction no script implemented invited an orchestrator to hand-adjust the script-
    computed Health Score on every commit-author fallback run (fixed confidence 0.5).
    """
    # Act
    text = (_PLUGIN / "skills" / "analyse" / "modes" / "vitality.md").read_text(encoding="utf-8")

    # Assert
    assert "effective weight" not in text
    assert "never folded into it or hand-adjusted" in text


def test_rework_never_changes_extractor_values() -> None:
    """Keep the Step 6 rework agent off the extractor-owned score, label and confidence, reading the right rubric.

    The rework prompt allowed a score or label change "unless the raw data clearly contradicts" it and pointed at the
    index rubric, which holds no axis text; a changed value left the report row disagreeing with the Health Score
    assembled in Step 3 and made two runs diverge on reviewer judgment.
    """
    # Act
    text = (_PLUGIN / "skills" / "analyse" / "modes" / "vitality-adversarial-rework.md").read_text(encoding="utf-8")

    # Assert
    assert "unless the raw data clearly contradicts" not in text
    assert "Never change the axis score, label or confidence" in text
    assert "vitality-scoring-group-a.md" in text
    assert "{axis_N_section_from_vitality_scoring_md}" not in text


@pytest.mark.parametrize("name", ["group-c", "repo-warden"])
@pytest.mark.parametrize("token", ["app/", "__typename", *sorted(vx._KNOWN_BOTS)])
def test_bot_rule_matches_the_extractor(name: str, token: str) -> None:
    """Document every bot signal the extractor uses — ``app/`` prefix, ``__typename``, each known automation name.

    The rubric described only the ``[bot]``/``-bot`` suffixes while the code also filtered GitHub Apps and nine named
    accounts, so a notes writer could not explain why a login was excluded.
    """
    # Act
    text = _FILES[name].read_text(encoding="utf-8")

    # Assert
    assert token in text


@pytest.mark.parametrize(
    ("source", "present", "absent"),
    [
        pytest.param(
            "Closed issues",
            'gh issue list --state closed --search "closed:>=CUTOFF_30D"',
            "last 3 years",
            id="closed-issues",
        ),
        pytest.param(
            "Closed PRs",
            'gh pr list --state closed --search "closed:>=CUTOFF_30D"',
            "pulls?state=closed",
            id="closed-prs",
        ),
        pytest.param(
            "Merged PRs 90d", 'gh pr list --state closed --search "merged:>=CUTOFF_90D"', "GET /repos", id="merged-prs"
        ),
        pytest.param(
            "CI runs", "actions/runs?branch={default}&status=completed", "newest 100 completed |", id="ci-runs"
        ),
        pytest.param(
            "CI runs",
            "push, schedule, workflow_dispatch and merge_group",
            "pull-request events excluded",
            id="ci-events",
        ),
        pytest.param("Open issues", "gh issue list --state open", "GET /repos", id="open-issues"),
        pytest.param("Open PRs", "gh pr list --state open", "GET /repos", id="open-prs"),
        pytest.param("Star history", "not collected", "GET /repos", id="star-history"),
    ],
)
def test_report_data_sources_name_the_real_fetch(source: str, present: str, absent: str) -> None:
    """Name each Data Sources row's real fetch: the search API for the windowed lists, the default branch for runs.

    The template cited REST ``pulls?state=closed`` for lists fetched through ``gh … --search`` and kept a "last 3 years"
    window the closed-issue fetch no longer uses, so a reader checking a count against the API queried the wrong
    endpoint. The open lists come from ``gh … list`` too, and no stargazers fetch runs at all.
    """
    # Arrange
    text = (_PLUGIN / "skills" / "analyse" / "templates" / "vitality-report.md").read_text(encoding="utf-8")

    # Act
    row = next(line for line in text.splitlines() if line.startswith(f"| {source} |"))

    # Assert
    assert present in row
    assert absent not in row


def test_axis8_partial_score_is_capped_at_the_yellow_maximum() -> None:
    """State the Axis 8 partial-score cap in the Group B rubric: a 🟡 label never carries a 🟢-range score."""
    # Act
    text = _FILES["group-b"].read_text(encoding="utf-8")

    # Assert
    assert "Score = min(10, partial_score)" not in text
    assert "capped at its label's band maximum (🟡 6, 🔴 3)" in text


def test_repo_warden_antipatterns_never_change_copied_values() -> None:
    """Every repo-warden antipattern is notes guidance; none asks the scorer to change a copied score.

    Two antipatterns told the scorer to cross-check before assigning a positive Axis 4 score and to score Axis 5 on the
    pass rate, contradicting the copy rule.
    """
    # Arrange
    text = _FILES["repo-warden"].read_text(encoding="utf-8")
    block = text.split("<antipatterns-to-flag>", 1)[1].split("</antipatterns-to-flag>", 1)[0]

    # Act
    bullets = [line for line in block.splitlines() if line.startswith("- **")]

    # Assert
    assert bullets
    assert all("(notes guidance)" in line for line in bullets)
    assert "assigning positive score" not in block

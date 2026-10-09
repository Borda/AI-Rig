"""Tests for the scoring rules ``bin/vitality_extract.py`` applies to every axis.

Two scorer models reading the same DATA_FILE must report the same band, score and confidence. The extractor computes
them, so each rule the rubric states in prose — band precedence when several band lines hold, boundary values, in-band
placement, listed-degrader confidence — is pinned here on the metric shapes where models used to diverge.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import vitality_extract as vx

_NOW = 1_800_000_000
_DAY = 86400


def _iso(days_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp ``days_ago`` days before ANALYSIS_NOW."""
    return datetime.fromtimestamp(_NOW - days_ago * _DAY, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _view(*records: dict) -> vx.DataView:
    """Build a DataView from ``{"type", "data", ...}`` records at the fixed ANALYSIS_NOW."""
    return vx.DataView(records={r["type"]: r for r in records}, now=_NOW)


def _readme(line: str, repo: str = "o/foo") -> dict:
    """README record of repository ``repo`` whose second line is ``line``, under a ``# foo`` title."""
    return {"type": "readme_content", "repo": repo, "data": f"# foo\n{line}\n"}


def _description(text: str) -> dict:
    """``repo_metadata`` record carrying only the repository description ``text``."""
    return {"type": "repo_metadata", "data": {"description": text}}


#: Axis 9 sub-signal metrics scoring 10, 5 or 0, per sub-signal; ``None`` leaves the sub-signal ⚪.
_AXIS9_INPUTS: dict[str, dict[int | None, dict]] = {
    "9A_pool_drift": {
        10: {"available": True, "shrinkage_ratio": 0.0, "pool_recent": 3},
        5: {"available": True, "shrinkage_ratio": 0.2, "pool_recent": 3},
        0: {"available": True, "shrinkage_ratio": 0.5, "pool_recent": 3},
        None: {"available": False},
    },
    "9B_merge_trend": {
        score: {
            "available": True,
            "trend_ratio": ratio,
            "merged_30d": 5,
            "merged_non_bot": 10,
            "truncated": False,
            "window_days_covered": 90,
        }
        for score, ratio in ((10, 1.0), (5, 1.5), (0, 3.0))
    }
    | {None: {"available": False, "merged_non_bot": 0, "truncated": False, "window_days_covered": None}},
    "9C_queue_depth": {
        10: {"available": True, "p90_age_days": 10.0},
        5: {"available": True, "p90_age_days": 60.0},
        0: {"available": True, "p90_age_days": 200.0},
        None: {"available": False},
    },
    "9D_automation": {
        10: {"auto_ratio": 0.1, "commits_sampled": 50},
        5: {"auto_ratio": 0.7, "commits_sampled": 50},
        0: {"auto_ratio": 0.95, "commits_sampled": 50},
        None: {"auto_ratio": None, "commits_sampled": 0},
    },
}


def _axis9(*scores: int | None) -> dict:
    """Axis 9 metrics whose sub-signals 9A, 9B, 9C and 9D score ``scores`` (``None`` = ⚪)."""
    return {name: _AXIS9_INPUTS[name][score] for name, score in zip(_AXIS9_INPUTS, scores, strict=True)}


def _axis1(**overrides: object) -> dict:
    """Axis 1 metrics of a fully responsive repository, with ``overrides`` applied."""
    return {
        "available": True,
        "issues_sampled": 20,
        "issues_eligible": 20,
        "prs_sampled": 20,
        "median_issue_response_days": 3.0,
        "median_pr_response_days": 1.0,
        "pct_responded_7d": 70.0,
        "pct_unresponded": 10.0,
    } | overrides


def _axis4(**overrides: object) -> dict:
    """Axis 4 metrics of a healthy queue; keys ``stale``/``close``/``merge``/``review``/``undefined`` override."""
    values = {"stale": 5.0, "close": 1.0, "merge": 0.9, "review": 90.0, "undefined": False} | overrides
    issues = {
        "open_available": True,
        "closed_available": True,
        "open_truncated": False,
        "closed_30d_is_lower_bound": False,
        "stale_pct": values["stale"],
        "close_rate": values["close"],
    }
    prs = {
        "open_available": True,
        "closed_available": True,
        "open_truncated": False,
        "closed_30d_is_lower_bound": False,
        "merge_rate": values["merge"],
    }
    review = {"available": True, "non_bot": 10, "coverage_pct": values["review"], "undefined": values["undefined"]}
    return {"issues": issues, "prs": prs, "review_coverage": review}


def _axis8(severity: dict, dep_config: str = "met", secret_open: int = 0, status: str = "available") -> dict:
    """Axis 8 metrics with the given Dependabot severities, dep-config state and open secret-scanning alerts."""
    return {
        "dependabot": {"status": status, "by_severity": severity, "at_limit": False},
        "secret_scanning": {"status": "available", "open_alerts": secret_open},
        "secondary": {"dep_config": {"state": dep_config, "why": ""}},
        "partial_score_strict": 0,
    }


class TestBandPlacement:
    """The shared band rule: worst band wins, else 🟢 when every clause holds, else 🟡; anchor + clauses held."""

    @pytest.mark.parametrize(
        ("red", "green", "band", "score"),
        [
            pytest.param({"r": True}, {"a": True, "b": True}, "🔴", 3, id="red-wins-over-all-green-clauses"),
            pytest.param({"r": False}, {"a": True, "b": True}, "🟢", 10, id="every-clause-green-is-10"),
            pytest.param({"r": False}, {"a": True, "b": False}, "🟡", 5, id="some-clause-fails"),
            pytest.param(
                {}, {"a": True, "b": True, "c": True, "d": True}, "🟢", 10, id="green-is-10-whatever-the-clause-count"
            ),
            pytest.param({"r": True}, {"a": True, "b": True, "c": True}, "🔴", 3, id="red-capped-at-3"),
        ],
    )
    def test_band_and_score(self, red: dict, green: dict, band: str, score: int) -> None:
        """Score 🟢 at 10 and 🔴/🟡 at the band anchor plus one per held 🟢 clause, capped at the band maximum.

        🟢 already requires every clause, so anchor-plus-clauses tied the top score to the clause count: axes 2 and 8,
        with two clauses each, could never exceed 9.
        """
        # Act
        result = vx.band_placement(red, green)

        # Assert
        assert (result["band"], result["score"]) == (band, score)


class TestAxis1Bands:
    """Axis 1 — overlapping 🟡/🔴 lines and boundary values."""

    @pytest.mark.parametrize(
        ("overrides", "band", "score"),
        [
            pytest.param({}, "🟢", 10, id="all-green"),
            pytest.param({"pct_responded_7d": 30.0}, "🔴", 3, id="fast-median-low-share-is-red"),
            pytest.param({"pct_responded_7d": 60.0}, "🟢", 10, id="pct-60-boundary-is-green"),
            pytest.param({"pct_responded_7d": 40.0}, "🟡", 6, id="pct-40-boundary-is-yellow"),
            pytest.param({"median_issue_response_days": 21.0}, "🟡", 6, id="median-21-boundary-is-yellow"),
            pytest.param({"median_issue_response_days": 7.0}, "🟢", 10, id="median-issue-7-boundary-is-green"),
            pytest.param({"median_issue_response_days": 7.01}, "🟡", 6, id="median-issue-past-7-is-yellow"),
            pytest.param({"median_pr_response_days": 5.0}, "🟢", 10, id="median-pr-5-boundary-is-green"),
            pytest.param({"median_pr_response_days": 5.01}, "🟡", 6, id="median-pr-past-5-is-yellow"),
            pytest.param({"pct_unresponded": 61.0}, "🔴", 3, id="unresponded-over-60-is-red"),
            pytest.param({"median_pr_response_days": None, "prs_sampled": 0}, "🟡", 6, id="no-pr-clause-is-unheld"),
        ],
    )
    def test_band(self, overrides: dict, band: str, score: int) -> None:
        """Pick the worst band whose line holds; a value on a shared boundary belongs to the better band."""
        # Act
        result = vx.score_axis1(_view(), _axis1(**overrides))

        # Assert
        assert (result["band"], result["score"]) == (band, score)

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"issues_sampled": 0, "issues_eligible": 0}, id="no-sampled-issue"),
            pytest.param({"issues_eligible": 0, "pct_responded_7d": None, "pct_unresponded": None}, id="all-censored"),
        ],
    )
    def test_no_judgeable_issue_is_unavailable(self, overrides: dict) -> None:
        """Mark the axis ⚪ when no issue is old enough or answered: no band line is decidable without issue metrics."""
        # Act
        result = vx.score_axis1(_view(), _axis1(median_issue_response_days=None, **overrides))

        # Assert
        assert (result["band"], result["score"], result["conf"]) == ("⚪", None, 0.0)

    @pytest.mark.parametrize(
        ("eligible", "conf"),
        [pytest.param(4, 0.8, id="four-eligible-of-20-sampled"), pytest.param(5, 1.0, id="five-eligible")],
    )
    def test_sample_degrader_reads_the_eligible_denominator(self, eligible: int, conf: float) -> None:
        """Degrade on the post-censoring denominator: 20 sampled with 16 too young leaves 4 issues deciding the band.

        Keying on ``issues_sampled`` reported 100% responded over 4 items at full confidence.
        """
        # Act
        result = vx.score_axis1(_view(), _axis1(issues_eligible=eligible, issues_too_young=20 - eligible))

        # Assert
        assert result["conf"] == conf


class TestAxis2Bands:
    """Axis 2 — the backport upgrade, the abandonment override and zero commits."""

    @pytest.mark.parametrize(
        ("records", "band", "score"),
        [
            pytest.param([{"type": "commits", "data": [_iso(1)] * 5}], "🟢", 10, id="active"),
            pytest.param([{"type": "commits", "data": [_iso(100)]}], "🔴", 1, id="stalled"),
            pytest.param(
                [
                    {"type": "commits", "data": [_iso(70), _iso(75), _iso(80)]},
                    {"type": "releases", "data": [{"tag": "v1.0.1", "published": _iso(50)}]},
                ],
                "🟡",
                4,
                id="backport-upgrades-red-to-yellow",
            ),
            pytest.param(
                [
                    {"type": "commits", "data": [_iso(70), _iso(75), _iso(80)]},
                    {"type": "releases", "data": [{"tag": "v1.0.1", "published": _iso(180)}]},
                ],
                "🟡",
                4,
                id="backport-release-180d-boundary-still-upgrades",
            ),
            pytest.param(
                [
                    {"type": "commits", "data": [_iso(70), _iso(75), _iso(80)]},
                    {"type": "releases", "data": [{"tag": "v1.0.1", "published": _iso(180.1)}]},
                ],
                "🔴",
                1,
                id="backport-release-past-180d-stays-red",
            ),
            pytest.param([{"type": "commits", "data": []}], "🔴", 1, id="empty-commit-list-is-zero-commits"),
            pytest.param(
                [
                    {"type": "commits", "data": [_iso(1)] * 5},
                    {"type": "readme_content", "data": "This project is deprecated."},
                ],
                "🔴",
                0,
                id="abandonment-override-scores-zero",
            ),
            pytest.param(
                [{"type": "commits", "data": [_iso(14)] * 5}], "🟢", 10, id="last-commit-14d-boundary-is-green"
            ),
            pytest.param([{"type": "commits", "data": [_iso(14.1)] * 5}], "🟡", 5, id="last-commit-past-14d-is-yellow"),
            pytest.param([], "⚪", None, id="missing-commit-list-is-unavailable"),
        ],
    )
    def test_band(self, records: list[dict], band: str, score: int | None) -> None:
        """Decide Axis 2 from commit recency and volume, with the backport upgrade and the ⛔ override."""
        # Act
        result = vx.axis_result("2", _view(*records))

        # Assert
        assert (result["band"], result["score"]) == (band, score)

    @pytest.mark.parametrize(
        ("extra", "override"),
        [
            pytest.param(
                {
                    "type": "repo_metadata",
                    "data": {"description": "Drop-in replacement for the deprecated foo library"},
                },
                None,
                id="description-naming-another-deprecated-thing",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nPython 2 support is deprecated as of v3.\n"},
                None,
                id="readme-deprecating-a-feature",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\n- deprecated: the `--fast` flag\n"},
                None,
                id="list-item-is-not-a-line-start-notice",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nAbandoned ideas live in the wiki.\n"},
                None,
                id="bare-keyword-is-not-a-self-reference",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nThis package is unmaintained.\n"},
                None,
                id="package-subject-may-be-a-sub-component",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\n> **Deprecated:** use bar instead.\n"},
                "⛔ self-declared discontinuation",
                id="line-start-deprecated-notice",
            ),
            pytest.param(
                {"type": "repo_metadata", "data": {"description": "No longer maintained, see bar"}},
                "⛔ self-declared discontinuation",
                id="description-no-longer-maintained",
            ),
            pytest.param(
                {"type": "repo_metadata", "data": {"description": "A formatter", "archived": True}},
                "⛔ repository archived",
                id="archived-repository",
            ),
            pytest.param(
                {"type": "repo_metadata", "data": {"description": "A formatter", "archived": False}},
                None,
                id="not-archived",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nThe 1.x branch is no longer maintained. Use 2.x.\n"},
                None,
                id="another-subject-no-longer-maintained",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nDeprecated: Python 3.8 support was dropped in 4.0\n"},
                None,
                id="plain-readme-deprecated-line-names-a-feature",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\n## Deprecated features\n"}, None, id="feature-heading"
            ),
            pytest.param(
                {"type": "repo_metadata", "data": {"description": "Deprecated APIs finder"}},
                None,
                id="description-naming-deprecated-things",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nThis project is no longer actively maintained.\n"},
                "⛔ self-declared discontinuation",
                id="this-project-no-longer-actively-maintained",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nThis repository has been archived.\n"},
                "⛔ self-declared discontinuation",
                id="this-repository-has-been-archived",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\nThis project is now deprecated.\n"},
                "⛔ self-declared discontinuation",
                id="this-project-is-now-deprecated",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# DEPRECATED\n"},
                "⛔ self-declared discontinuation",
                id="title-heading-holding-only-the-status",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo (DEPRECATED)\n"},
                "⛔ self-declared discontinuation",
                id="title-with-the-status-in-parentheses",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo — DEPRECATED\n"},
                "⛔ self-declared discontinuation",
                id="title-with-the-status-after-a-dash",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo [ARCHIVED]\n"},
                "⛔ self-declared discontinuation",
                id="title-with-the-status-in-brackets",
            ),
            pytest.param(
                {"type": "readme_content", "data": '<h1 align="center">DEPRECATED</h1>\n'},
                "⛔ self-declared discontinuation",
                id="html-title-holding-only-the-status",
            ),
            pytest.param(
                {"type": "readme_content", "data": '<h2 align="center">The Uncompromising Code Formatter</h2>\n'},
                None,
                id="html-subtitle-without-a-status",
            ),
            pytest.param(
                {"type": "readme_content", "data": "foo (deprecated)\n================\n"},
                "⛔ self-declared discontinuation",
                id="setext-title-with-the-status",
            ),
            pytest.param(
                _readme("> [!WARNING]\n> **Deprecated**"),
                "⛔ self-declared discontinuation",
                id="alert-banner-holding-only-the-status",
            ),
            pytest.param(
                _readme("No longer maintained. Use bar instead."),
                "⛔ self-declared discontinuation",
                id="status-sentence-then-a-redirect",
            ),
            pytest.param(_readme("## DEPRECATED"), None, id="bare-section-heading"),
            pytest.param(_readme("# Deprecated APIs"), None, id="title-status-qualifies-a-noun"),
            pytest.param(
                _readme("Deprecated: use --new-flag instead of --old-flag"),
                None,
                id="redirect-to-a-flag-is-a-feature-note",
            ),
            pytest.param(
                _readme("### Deprecated: use `new_fn` instead of `old_fn`"),
                None,
                id="redirect-naming-what-it-replaces-is-a-feature-note",
            ),
            pytest.param(_readme("Deprecated use of `foo()` now warns."), None, id="use-as-a-noun-is-a-feature-note"),
            pytest.param(
                _readme("**Deprecated:** see below for removed flags"), None, id="see-below-points-into-the-readme"
            ),
            pytest.param(
                _readme("Deprecated, see CHANGELOG for the migration of old APIs."),
                None,
                id="see-with-a-feature-clause",
            ),
            pytest.param(
                _readme("Archived. Releases before 2.0 live in the old repo."),
                None,
                id="status-sentence-then-another-subject",
            ),
            pytest.param(
                {"type": "readme_content", "data": "x" * 486 + "\n**Deprecated APIs** are listed in the docs.\n"},
                None,
                id="line-the-500-byte-cut-ends",
            ),
            pytest.param(
                {
                    "type": "readme_content",
                    "data": '<img src="' + "u" * 475 + '"> **Deprecated APIs** are listed below.\n',
                },
                None,
                id="first-line-longer-than-the-500-byte-cut",
            ),
            pytest.param(
                _readme("**Deprecated:** Python 3.8 support was dropped in 4.0."),
                None,
                id="bold-deprecated-colon-names-a-feature",
            ),
            pytest.param(_readme("_Deprecated_ options are listed below"), None, id="italic-status-qualifies-a-noun"),
            pytest.param(_readme("[Deprecated] flags: --old"), None, id="bracket-status-qualifies-a-noun"),
            pytest.param(_readme("(deprecated) old_api()"), None, id="paren-status-qualifies-a-call"),
            pytest.param(_readme("- Deprecated"), None, id="dash-bullet-alone"),
            pytest.param(_readme("* Deprecated"), None, id="star-bullet-alone"),
            pytest.param(_readme("## Archived"), None, id="bare-archived-heading"),
            pytest.param(_readme("| Deprecated |"), None, id="table-cell"),
            pytest.param(
                _readme("foo.bar: this module is deprecated, use foo.baz"), None, id="module-subject-is-a-component"
            ),
            pytest.param(
                _readme("**Note:** this package is deprecated on Python 2."), None, id="package-subject-on-a-platform"
            ),
            pytest.param(
                _readme("Formatter. Note: the old black is deprecated", repo="psf/black"),
                None,
                id="common-word-name-inside-other-text",
            ),
            pytest.param(
                _readme("This project is not actively maintained."),
                "⛔ self-declared discontinuation",
                id="this-project-not-actively-maintained",
            ),
            pytest.param(
                _readme("This project is not maintained anymore."),
                "⛔ self-declared discontinuation",
                id="this-project-not-maintained-anymore",
            ),
            pytest.param(
                _readme("We are no longer maintaining this project."),
                "⛔ self-declared discontinuation",
                id="no-longer-maintaining-this-project",
            ),
            pytest.param(
                _readme("# ⚠️ DEPRECATED"), "⛔ self-declared discontinuation", id="heading-with-a-warning-sign"
            ),
            pytest.param(
                _readme("## Deprecated — use bar instead"),
                "⛔ self-declared discontinuation",
                id="heading-with-a-redirect",
            ),
            pytest.param(_readme("DEPRECATED"), "⛔ self-declared discontinuation", id="status-alone-on-its-line"),
            pytest.param(
                _readme("Black is deprecated in favor of ruff.", repo="psf/black"),
                "⛔ self-declared discontinuation",
                id="common-word-name-as-sentence-subject",
            ),
            pytest.param(
                _description("Deprecated in favor of bar"),
                "⛔ self-declared discontinuation",
                id="description-deprecated-in-favor-of",
            ),
            pytest.param(
                _description("DEPRECATED, please use bar"),
                "⛔ self-declared discontinuation",
                id="description-please-use",
            ),
            pytest.param(
                _description("Unmaintained fork of foo"),
                "⛔ self-declared discontinuation",
                id="description-unmaintained-fork",
            ),
            pytest.param(
                _description("[UNMAINTAINED] foo"), "⛔ self-declared discontinuation", id="description-status-tag"
            ),
            pytest.param(
                _description("Deprecated library finder"), None, id="description-tool-for-deprecated-libraries"
            ),
            pytest.param(_description("Deprecated project template"), None, id="description-template-subject"),
            pytest.param(
                {"type": "readme_content", "data": "# foo\n**DEPRECATED** use bar\n"},
                "⛔ self-declared discontinuation",
                id="bold-deprecated-banner",
            ),
            pytest.param(
                {"type": "readme_content", "data": "# foo\n⚠️ Deprecated\n"},
                "⛔ self-declared discontinuation",
                id="warning-sign-deprecated",
            ),
            pytest.param(
                {"type": "repo_metadata", "data": {"description": "DEPRECATED - use bar"}},
                "⛔ self-declared discontinuation",
                id="description-leading-deprecated",
            ),
            pytest.param(
                {
                    "type": "readme_content",
                    "repo": "mozilla/bleach",
                    "data": "Bleach\n\n**NOTE: 2023-01-23: Bleach is deprecated.** See issue 698\n",
                },
                "⛔ self-declared discontinuation",
                id="repository-name-is-deprecated",
            ),
        ],
    )
    def test_abandonment_override_needs_a_self_reference(self, extra: dict, override: str | None) -> None:
        """Zero an active repository only on an archived flag or a statement that the repository itself is discontinued.

        Matching the bare word ``deprecated`` anywhere in the description scored an active formatter ⛔ 0 because its
        description named a deprecated library it replaces; "no longer maintained" about an old branch and a plain
        "Deprecated:" line listing a dropped feature did the same. Markup shape (bold, bracket, heading) never tells the
        repository's status from a feature's, so a banner line fires on its content: the status alone, the status beside
        the README title, or the status with a redirect to one replacement; a line naming another subject, a list item,
        a table row, a bare section heading and the line the 500-byte cut ends never fire.
        """
        # Arrange
        view = _view({"type": "commits", "data": [_iso(1)] * 20}, extra)

        # Act
        result = vx.axis_result("2", view)

        # Assert
        assert result.get("override") == override
        assert result["score"] == (10 if override is None else 0)

    def test_missing_commits_still_honour_the_archived_flag(self) -> None:
        """Score an archived repository ⛔ 0 even without a commit list; only an undecided axis is ⚪."""
        # Act
        result = vx.axis_result("2", _view({"type": "repo_metadata", "data": {"archived": True}}))

        # Assert
        assert (result["band"], result["score"], result["override"]) == ("🔴", 0, "⛔ repository archived")


class TestAxis3Bands:
    """Axis 3 — overlapping lines, undefined retention and the Axis 2 cross-reference."""

    @staticmethod
    def _metrics(bus: int | None, top: float | None, retention: float | None) -> dict:
        """Axis 3 metrics with available contributor stats."""
        return {
            "stats_status": "available",
            "bus_factor": bus,
            "top_contributor_pct": top,
            "retention_pct": retention,
            "contributors": 10,
            "all_90d_weeks_zero": False,
            "fallback": {"approx_bus_factor": 3},
        }

    @pytest.mark.parametrize(
        ("bus", "top", "retention", "commits", "band", "score"),
        [
            pytest.param(3, 40.0, 60.0, [_iso(1)] * 5, "🟢", 10, id="all-green"),
            pytest.param(2, 40.0, 25.0, [_iso(1)] * 5, "🔴", 2, id="bus-2-and-low-retention-worst-wins"),
            pytest.param(3, 40.0, None, [_iso(1)] * 5, "🟡", 6, id="undefined-retention-caps-yellow"),
            pytest.param(1, 40.0, 60.0, [_iso(100)], "🔴", 3, id="bus-1-with-axis-2-red"),
            pytest.param(1, 40.0, 60.0, [_iso(1)] * 5, "🟡", 6, id="bus-1-with-axis-2-green"),
            pytest.param(3, 50.0, 60.0, [_iso(1)] * 5, "🟢", 10, id="top-contributor-50-boundary-is-green"),
            pytest.param(3, 50.1, 60.0, [_iso(1)] * 5, "🟡", 6, id="top-contributor-past-50-is-yellow"),
        ],
    )
    def test_band(
        self, bus: int, top: float, retention: float | None, commits: list[str], band: str, score: int
    ) -> None:
        """Read the Axis 2 band from the extractor so the bus-factor-1 clause is decidable inside Group C."""
        # Arrange
        view = _view({"type": "commits", "data": commits})

        # Act
        result = vx.score_axis3(view, self._metrics(bus, top, retention))

        # Assert
        assert (result["band"], result["score"]) == (band, score)

    def test_fallback_uses_fixed_confidence(self) -> None:
        """Score the commit-author fallback with only the bus-factor clause decidable, at fixed confidence 0.5."""
        # Arrange
        records = (
            {"type": "contributor_stats", "data": None, "partial": True, "202_pending": True},
            {"type": "commits_50", "data": [{"author": name} for name in ("a", "b", "c", "d")]},
            {"type": "commits", "data": [_iso(1)] * 5},
        )

        # Act
        result = vx.axis_result("3", _view(*records))

        # Assert
        assert (result["band"], result["score"], result["conf"]) == ("🟡", 5, 0.5)
        assert result["conf_fixed"].startswith("contributor stats 202_pending")

    def test_dormant_90d_window_is_red(self) -> None:
        """Score a 90-day window without a non-bot commit 🔴, never the 🟡 an all-undefined clause set defaulted to."""
        # Arrange
        metrics = self._metrics(None, None, None) | {"all_90d_weeks_zero": True}

        # Act
        result = vx.score_axis3(_view({"type": "commits", "data": [_iso(1)] * 5}), metrics)

        # Assert
        assert (result["band"], result["score"], result["red_held"]) == (
            "🔴",
            1,
            ["no non-bot commit in 90d (all_90d_weeks_zero)"],
        )


class TestAxis4Bands:
    """Axis 4 — worst-of composite and shared range boundaries."""

    @pytest.mark.parametrize(
        ("overrides", "band", "score"),
        [
            pytest.param({}, "🟢", 10, id="all-green"),
            pytest.param({"close": 0.8}, "🟢", 10, id="close-rate-0.8-boundary-is-green"),
            pytest.param({"merge": 0.7, "review": 80.0}, "🟢", 10, id="merge-0.7-review-80-boundary-is-green"),
            pytest.param({"stale": 35.0}, "🔴", 3, id="one-red-dimension-is-red"),
            pytest.param({"stale": 30.0}, "🟡", 6, id="stale-30-boundary-is-yellow"),
            pytest.param({"stale": 10.0}, "🟢", 10, id="stale-10-boundary-is-green"),
            pytest.param({"stale": 10.1}, "🟡", 6, id="stale-past-10-is-yellow"),
            pytest.param({"close": 0.4}, "🟡", 6, id="close-rate-0.4-boundary-is-yellow"),
            pytest.param({"undefined": True}, "🟡", 6, id="undefined-review-coverage-is-yellow"),
            pytest.param({"close": None}, "🟡", 6, id="null-close-rate-is-not-red"),
        ],
    )
    def test_band(self, overrides: dict, band: str, score: int) -> None:
        """Turn the axis 🔴 when any dimension is 🔴; a null ratio neither holds a clause nor triggers 🔴."""
        # Act
        result = vx.score_axis4(_view(), _axis4(**overrides))

        # Assert
        assert (result["band"], result["score"]) == (band, score)

    @pytest.mark.parametrize(
        ("closed", "printed", "green_held", "red_held"),
        [
            pytest.param(159, 0.8, False, False, id="159-of-200-prints-0.8-misses-the-green-clause"),
            pytest.param(79, 0.4, False, True, id="79-of-200-prints-0.4-holds-the-red-condition"),
        ],
    )
    def test_close_rate_thresholds_read_the_unrounded_value(
        self, closed: int, printed: float, green_held: bool, red_held: bool
    ) -> None:
        """Compare the close rate to its band lines unrounded; only the printed metric is rounded.

        Rounding first made 159/200 = 0.795 read as 0.8 and hold the ``≥0.8`` clause, and 79/200 = 0.395 read as 0.4
        and escape 🔴.
        """
        # Arrange
        opened = [{"createdAt": _iso(1), "updatedAt": _iso(1)}] * (200 - closed)
        closed_issues = [{"createdAt": _iso(2), "closedAt": _iso(1)}] * closed
        records = ({"type": "open_issues", "data": opened}, {"type": "closed_issues", "data": closed_issues})

        # Act
        result = vx.axis_result("4", _view(*records))

        # Assert
        assert result["issues"]["close_rate"] == printed
        assert ("close_rate ≥0.8" in result["clauses_held"]) is green_held
        assert ("close_rate <0.4" in result["red_held"]) is red_held


class TestAxis8Bands:
    """Axis 8 — alert bands, secret-scanning alerts and the partial-score label map."""

    @pytest.mark.parametrize(
        ("severity", "secret_open", "band", "score"),
        [
            pytest.param({}, 0, "🟢", 10, id="no-alerts"),
            pytest.param({"high": 1}, 0, "🟡", 5, id="one-high"),
            pytest.param({"high": 6}, 0, "🔴", 2, id="six-high-both-lines-hold-red-wins"),
            pytest.param({"critical": 1}, 0, "🔴", 2, id="one-critical"),
            pytest.param({}, 1, "🟡", 5, id="secret-scanning-alert-counts-as-high"),
            pytest.param({"high": 4}, 1, "🔴", 2, id="four-high-plus-secret-reaches-five"),
        ],
    )
    def test_alert_band(self, severity: dict, secret_open: int, band: str, score: int) -> None:
        """Band the alert counts with the worst band winning; an open secret-scanning alert is a high alert."""
        # Act
        result = vx.score_axis8(_view(), _axis8(severity, secret_open=secret_open))

        # Assert
        assert (result["band"], result["score"]) == (band, score)

    @pytest.mark.parametrize(
        ("partial", "band", "score"),
        [
            pytest.param(10, "🟡", 6, id="max-partial-capped-at-yellow-max"),
            pytest.param(7, "🟡", 6, id="config-and-commits-capped-at-yellow-max"),
            pytest.param(6, "🟡", 6, id="six-is-yellow-max"),
            pytest.param(5, "🟡", 5, id="five-is-yellow"),
            pytest.param(4, "🟡", 4, id="four-is-yellow"),
            pytest.param(3, "🔴", 3, id="three-is-red"),
            pytest.param(0, "🔴", 0, id="no-signal-is-red-zero"),
        ],
    )
    def test_partial_score_label(self, partial: int, band: str, score: int) -> None:
        """Label the 403 partial score ≥4 🟡 else 🔴, never 🟢, and keep the score in the label's band range.

        Uncapped, partial points of 10 scored 10 under a yellow label: a green-range number, counted in the Health Score
        exactly like a verified green.
        """
        # Arrange
        metrics = _axis8({}, status="403") | {"partial_score_strict": partial}

        # Act
        result = vx.score_axis8(_view(), metrics)

        # Assert
        assert (result["band"], result["score"], result["conf"]) == (band, score, 0.4)

    @pytest.mark.parametrize(
        ("alerts", "band", "score", "conf"),
        [
            pytest.param("403", "🟡", 6, 0.4, id="token-without-alert-access"),
            pytest.param([], "🟢", 10, 1.0, id="admin-token-no-alerts"),
            pytest.param([{"security_advisory": {"severity": "high"}}], "🟡", 5, 1.0, id="admin-token-one-high"),
        ],
    )
    def test_token_access_never_lifts_the_unverified_score(
        self, alerts: object, band: str, score: int, conf: float
    ) -> None:
        """Score one repository through both token paths; the unverified path tops out at the 🟡 maximum.

        Every secondary signal is present, so the partial points reach 10: a non-admin run used to score 🟡 10 while an
        admin run that saw one high alert scored 🟡 5 — the run that knew less scored higher.
        """
        # Arrange
        records = (
            {"type": "root_contents", "data": ["SECURITY.md", ".github"]},
            {"type": "github_dir", "data": ["dependabot.yml"]},
            {"type": "commits_50", "data": [{"message": "Bump foo from 1 to 2", "date": _iso(3), "author": "x"}]},
            {"type": "dependabot_alerts", "data": alerts},
            {"type": "secret_scanning_alerts", "data": []},
        )

        # Act
        result = vx.axis_result("8", _view(*records))

        # Assert
        assert (result["band"], result["score"], result["conf"]) == (band, score, conf)
        assert result["partial_score_strict"] == 10


class TestConfidence:
    """Listed degraders replace the per-checkpoint -0.05 for the checkpoints they cover; never both."""

    def test_axis5_partial_content_replaces_per_checkpoint_deduction(self) -> None:
        """Deduct -0.1 once for partially read workflows and -0.05 only for the uncovered runs checkpoint.

        Adding -0.05 per covered checkpoint on top gave 0.75 for one model and 0.85 for the other on the same data.
        """
        # Arrange
        records = (
            {"type": "workflows_list", "data": ["a.yml", "b.yml"]},
            {
                "type": "workflow_files",
                "listed": 2,
                "fetched": 1,
                "partial": True,
                "data": "--- workflow: a.yml ---\nrun: pytest\n",
            },
        )

        # Act
        result = vx.axis_result("5", _view(*records))

        # Assert
        assert result["conf"] == 0.85
        assert [d["cause"] for d in result["conf_degraders"]] == [
            "workflow_files partial (some files unread)",
            "checkpoint 5 indeterminate",
        ]

    def test_axis6_unfetched_contributing_costs_one_listed_degrader(self) -> None:
        """Deduct -0.1 for CONTRIBUTING listed but unread, not an extra -0.05 for each of checkpoints 7–9."""
        # Arrange
        records = (
            {"type": "readme_content", "data": "## Install\npip install x\n## Usage\n" + "x" * 600},
            {"type": "root_contents", "data": ["README.md", "CHANGELOG.md", "docs", "examples", ".github"]},
            {"type": "github_dir", "data": ["CONTRIBUTING.md"]},
            {"type": "changelog_headings", "data": {"head": [], "headings": [f"## 1.0 - {_iso(10)[:10]}"]}},
        )

        # Act
        result = vx.axis_result("6", _view(*records))

        # Assert
        assert result["conf"] == 0.9

    def test_axis7_unfetched_branch_flag_costs_one_listed_degrader(self) -> None:
        """Deduct -0.1 for the missing protection flag and nothing for the admin-only rules record."""
        # Arrange
        records = (
            {"type": "root_contents", "data": ["LICENSE", "SECURITY.md", "CODE_OF_CONDUCT.md", ".github"]},
            {"type": "github_dir", "data": ["CODEOWNERS", "CONTRIBUTING.md"]},
        )

        # Act
        result = vx.axis_result("7", _view(*records))

        # Assert
        assert result["conf"] == 0.9
        assert [d["cause"] for d in result["conf_degraders"]] == [
            "default_branch_status missing (protection flag not fetched)"
        ]

    def test_axis5_run_count_degrader_skips_a_repo_without_workflows(self) -> None:
        """Skip the short-run-list degrader when no workflow file exists: checkpoint 5 is then decided, not unstable.

        A repository with no CI scored 🔴 0 at confidence 0.9, as if its pass rate were merely noisy.
        """
        # Arrange
        records = (
            {"type": "root_contents", "data": ["README.md", "src"]},
            {"type": "ci_workflows", "data": {"count": 0, "names": [], "workflows": []}},
            {"type": "ci_runs", "data": []},
        )

        # Act
        result = vx.axis_result("5", _view(*records))

        # Assert
        assert (result["band"], result["score"], result["conf"]) == ("🔴", 0, 1.0)

    def test_axis5_full_run_sample_is_not_a_degrader(self) -> None:
        """Keep confidence 1.0 when 21 runs came back: that proves ≥21 runs exist, so the newest 20 are complete."""
        # Arrange
        records = (
            {"type": "workflows_list", "data": ["ci.yml"]},
            {"type": "workflow_files", "listed": 1, "fetched": 1, "data": "--- workflow: ci.yml ---\npytest ruff\n"},
            {"type": "ci_runs", "partial": True, "data": [{"conclusion": "success"}] * 21},
        )

        # Act
        result = vx.axis_result("5", _view(*records))

        # Assert
        assert (result["band"], result["score"], result["conf"]) == ("🟢", 8, 1.0)

    @pytest.mark.parametrize(
        ("partial", "conf"),
        [pytest.param(True, 0.9, id="truncated-short-window"), pytest.param(False, 1.0, id="complete-short-window")],
    )
    def test_axis9_window_degrader_needs_truncation(self, partial: bool, conf: float) -> None:
        """Deduct the merged-PR window degrader only when the list is truncated AND covers under 90 days."""
        # Arrange
        merged = [{"author": {"login": f"u{i}"}, "createdAt": _iso(27), "mergedAt": _iso(26)} for i in range(6)]
        records = (
            {"type": "contributor_stats", "data": [{"author": "a", "weeks": [{"c": 1}] * 52}]},
            {"type": "merged_prs_90d", "partial": partial, "data": merged},
            {"type": "commits_50", "data": [{"author": "a", "message": "fix"}] * 10},
        )

        # Act
        result = vx.axis_result("9", _view(*records))

        # Assert
        assert result["conf"] == conf


class TestAxis9Boundaries:
    """Axis 9 sub-signal boundaries belong to the better band, like every band-only clause."""

    @pytest.mark.parametrize(
        ("scorer", "sub", "score"),
        [
            pytest.param(vx._queue_depth_score, {"available": True, "p90_age_days": 30.0}, 10, id="9c-p90-30-is-green"),
            pytest.param(
                vx._queue_depth_score, {"available": True, "p90_age_days": 30.1}, 5, id="9c-p90-past-30-is-yellow"
            ),
            pytest.param(vx._automation_score, {"auto_ratio": 0.5}, 10, id="9d-ratio-0.50-is-green"),
            pytest.param(vx._automation_score, {"auto_ratio": 0.52}, 5, id="9d-ratio-past-0.50-is-yellow"),
        ],
    )
    def test_boundary_scores(self, scorer: object, sub: dict, score: int) -> None:
        """Score a P90 age of exactly 30 days and a bot-bump share of exactly 0.50 as 🟢 (10)."""
        # Act
        result = scorer(sub)  # type: ignore[operator]

        # Assert
        assert result == score

    @pytest.mark.parametrize(
        ("subs", "band", "score"),
        [
            pytest.param((10, 10, 5, 0), "🟡", 6, id="four-subs-mean-6.25-capped-at-yellow-max"),
            pytest.param((10, 10, None, 0), "🟡", 6, id="three-subs-mean-6.67-capped-at-yellow-max"),
            pytest.param((10, 0, None, 0), "🔴", 3, id="three-subs-mean-3.33-capped-at-red-max"),
            pytest.param((5, 5, 5, 5), "🟡", 5, id="yellow-mean-below-the-cap-kept"),
            pytest.param((5, 0, 0, 0), "🔴", 1.25, id="red-mean-below-the-cap-kept"),
            pytest.param((10, 10, 10, 5), "🟢", 8.75, id="green-keeps-the-mean"),
        ],
    )
    def test_score_is_capped_at_the_band_maximum(self, subs: tuple, band: str, score: float) -> None:
        """Cap a 🟡 Axis 9 mean at 6 and a 🔴 mean at 3, like every other axis; 🟢 keeps the mean.

        Live roboflow/rf-detr scored 🟡 6.25 (sub-signals 10/10/5/0): a 🟡 trajectory out-scored the 🟡 maximum every
        other axis respects, the inversion the Axis 8 partial-score cap was introduced to remove.
        """
        # Act
        result = vx.score_axis9(_view(), _axis9(*subs))

        # Assert
        assert (result["band"], result["score"]) == (band, score)

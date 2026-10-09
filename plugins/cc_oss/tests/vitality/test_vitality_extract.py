"""Tests for ``bin/vitality_extract.py``.

Each test builds a handful of DATA_FILE records relative to a fixed ANALYSIS_NOW and asserts the metrics one axis
extractor derives from them, so a rubric field the agent scores from can never silently change meaning.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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


def _weeks(*counts: int) -> list[dict]:
    """Contributor-stats weekly buckets holding the given commit counts, oldest first."""
    return [{"w": index, "a": 0, "d": 0, "c": count} for index, count in enumerate(counts)]


def _states(axis: dict) -> dict[str, str]:
    """Checkpoint number → state of one checkpoint axis."""
    return {number: checkpoint["state"] for number, checkpoint in axis["checkpoints"].items()}


@pytest.mark.parametrize(
    ("login", "flag", "expected"),
    [
        pytest.param("dependabot[bot]", None, True, id="bot-suffix"),
        pytest.param("release-bot", None, True, id="dash-bot-suffix"),
        pytest.param("app/pre-commit-ci", None, True, id="github-app-prefix"),
        pytest.param("renovate", None, True, id="known-name"),
        pytest.param("alice", True, True, id="is-bot-flag-wins"),
        pytest.param("codex", None, False, id="unknown-name-counts-human"),
        pytest.param(None, None, False, id="deleted-user-human"),
    ],
)
def test_is_bot(login: str | None, flag: bool | None, expected: bool) -> None:
    """Classify automation accounts conservatively, under-filtering unknown names."""
    # Act
    result = vx.is_bot(login, flag)

    # Assert
    assert result is expected


def test_load_records_keeps_last_per_type_and_skips_malformed(capsys: pytest.CaptureFixture[str]) -> None:
    """Take the last record of each type and warn about lines that are not JSON."""
    # Arrange
    text = '{"type": "a", "data": 1}\n{broken\n\n{"type": "a", "data": 2}\n{"no_type": 1}\n'

    # Act
    records = vx.load_records(text)

    # Assert
    assert records == {"a": {"type": "a", "data": 2}}
    assert "skipped malformed line 2" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("override", "expected"),
    [pytest.param(None, 1_700_000_000, id="record-timestamp"), pytest.param(123, 123, id="explicit-override")],
)
def test_analysis_now_prefers_override_then_record_timestamp(override: int | None, expected: int) -> None:
    """Measure time windows from the explicit override, else the scrape timestamp."""
    # Arrange
    records = {
        "readme_content": {"type": "readme_content", "data": "x"},
        "commits": {"type": "commits", "timestamp": 1_700_000_000, "data": []},
    }

    # Act
    result = vx.extract(records, vx.AxisGroup.A, override)

    # Assert
    assert result["analysis_now"] == expected


class TestGroupA:
    """Axes 1, 2, 5 and 6."""

    def test_axis1_counts_only_non_author_first_events(self) -> None:
        """Count an issue answered by someone else and treat an author-first comment as unresponded."""
        # Arrange
        payload = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "createdAt": _iso(10),
                                "author": {"login": "u1"},
                                "comments": {"nodes": [{"createdAt": _iso(8), "author": {"login": "m"}}]},
                            },
                            {
                                "createdAt": _iso(10),
                                "author": {"login": "u2"},
                                "comments": {"nodes": [{"createdAt": _iso(9), "author": {"login": "u2"}}]},
                            },
                        ]
                    },
                    "pullRequests": {
                        "nodes": [
                            {
                                "createdAt": _iso(5),
                                "author": {"login": "c"},
                                "reviews": {"nodes": [{"createdAt": _iso(4), "author": {"login": "m"}}]},
                                "comments": {"nodes": []},
                            }
                        ]
                    },
                }
            }
        }

        # Act
        axis = vx.axis1_responsiveness(_view({"type": "responsiveness_gql", "data": payload}))

        # Assert
        assert axis["issues_sampled"] == 2
        assert axis["issues_responded"] == 1
        assert axis["median_issue_response_days"] == 2.0
        assert axis["pct_responded_7d"] == 50.0
        assert axis["pct_unresponded"] == 50.0
        assert axis["median_pr_response_days"] == 1.0

    @pytest.mark.parametrize(
        "bot_author",
        [
            pytest.param({"login": "codecov", "__typename": "Bot"}, id="typename-bot"),
            pytest.param({"login": "codecov"}, id="known-login-without-typename"),
        ],
    )
    def test_axis1_skips_bot_responses(self, bot_author: dict) -> None:
        """A bot comment seconds after PR creation is not a response; the first human event is.

        Coverage, CLA and security apps comment on every PR at once, and GraphQL drops their ``[bot]`` suffix, so
        counting them made every PR look answered in 0 days and passed the Axis 1 PR-response clause falsely.
        """
        # Arrange
        pr = {
            "createdAt": _iso(5),
            "author": {"login": "c"},
            "comments": {"nodes": [{"createdAt": _iso(5), "author": bot_author}]},
            "reviews": {"nodes": [{"createdAt": _iso(3), "author": {"login": "m", "__typename": "User"}}]},
        }
        payload = {"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": [pr]}}}}

        # Act
        axis = vx.axis1_responsiveness(_view({"type": "responsiveness_gql", "data": payload}))

        # Assert
        assert axis["median_pr_response_days"] == 2.0

    def test_axis1_censors_young_unanswered_issues(self) -> None:
        """Drop an unanswered issue younger than 7 days from both percentages; its 7-day outcome is still open.

        Counting it as unanswered deflated busy repositories, whose newest sampled issues are hours old.
        """
        # Arrange
        answered = {
            "createdAt": _iso(10),
            "author": {"login": "u1"},
            "comments": {"nodes": [{"createdAt": _iso(8), "author": {"login": "m"}}]},
        }
        young = {"createdAt": _iso(1), "author": {"login": "u2"}, "comments": {"nodes": []}}
        payload = {"data": {"repository": {"issues": {"nodes": [answered, young]}, "pullRequests": {"nodes": []}}}}

        # Act
        axis = vx.axis1_responsiveness(_view({"type": "responsiveness_gql", "data": payload}))

        # Assert
        assert (axis["issues_too_young"], axis["pct_responded_7d"], axis["pct_unresponded"]) == (1, 100.0, 0.0)

    def test_axis1_counts_unanswered_items_with_full_event_lists(self) -> None:
        """Count unanswered items whose fetched event list is full, since a human reply may lie past the cap."""
        # Arrange
        events = {"nodes": [{"createdAt": _iso(9), "author": {"login": "u1"}}] * 10}
        issue = {"createdAt": _iso(10), "author": {"login": "u1"}, "comments": events}
        payload = {"data": {"repository": {"issues": {"nodes": [issue]}, "pullRequests": {"nodes": []}}}}

        # Act
        axis = vx.axis1_responsiveness(_view({"type": "responsiveness_gql", "data": payload}))

        # Assert
        assert axis["issues_unresponded_at_event_cap"] == 1

    def test_axis2_missing_commits_are_null_not_zero(self) -> None:
        """Report 30/90-day commit counts as null when the commit list was not fetched, never as a measured 0."""
        # Act
        axis = vx.axis2_maintenance(_view())

        # Assert
        assert (axis["commits_available"], axis["commits_30d"], axis["commits_90d"]) == (False, None, None)

    def test_axis5_workflow_file_name_never_meets_a_content_checkpoint(self) -> None:
        """Ignore the ``--- workflow: <name> ---`` header: ``codeql.yml`` running only ``make check`` is no scan."""
        # Arrange
        content = {
            "type": "workflow_files",
            "listed": 1,
            "fetched": 1,
            "data": "--- workflow: codeql.yml ---\nrun: make check\n",
        }

        # Act
        axis = vx.axis5_ci(_view({"type": "workflows_list", "data": ["codeql.yml"]}, content))

        # Assert
        assert axis["checkpoints"]["4"]["state"] == "unmet"

    @pytest.mark.parametrize(
        ("records", "state"),
        [
            pytest.param(
                [
                    {"type": "github_dir", "data": ["ISSUE_TEMPLATE"]},
                    {
                        "type": "ci_workflows",
                        "data": {"workflows": [{"path": ".github/workflows/ci.yml", "state": "active"}]},
                    },
                ],
                "unmet",
                id="listing-without-workflows-beats-registry",
            ),
            pytest.param(
                [
                    {
                        "type": "ci_workflows",
                        "data": {"workflows": [{"path": ".github/workflows/ci.yml", "state": "deleted"}]},
                    }
                ],
                "unmet",
                id="deleted-registry-entry-is-not-ci",
            ),
            pytest.param(
                [
                    {
                        "type": "ci_workflows",
                        "data": {"workflows": [{"path": ".github/workflows/ci.yml", "state": "active"}]},
                    }
                ],
                "met",
                id="active-registry-entry-without-listing",
            ),
            pytest.param(
                [{"type": "github_dir", "data": ["workflows"]}, {"type": "workflows_list", "data": ["README.md"]}],
                "unmet",
                id="workflows-dir-without-yaml-runs-nothing",
            ),
            pytest.param(
                [{"type": "workflows_list", "data": ["README.md", "ci.YAML"]}], "met", id="yaml-file-beside-a-readme"
            ),
        ],
    )
    def test_axis5_ci_present_trusts_the_listing_over_the_registry(self, records: list[dict], state: str) -> None:
        """Decide checkpoint 1 from the default-branch listing when known; else count non-deleted registry files."""
        # Act
        checkpoint = vx.axis5_ci(_view(*records))["checkpoints"]["1"]

        # Assert
        assert checkpoint["state"] == state

    def test_axis1_reports_unavailable_without_dataset(self) -> None:
        """Mark responsiveness unavailable instead of reporting zero samples as measured."""
        # Act
        axis = vx.axis1_responsiveness(_view())

        # Assert
        assert axis == {"available": False, "reason": "responsiveness_gql missing"}

    def test_axis2_counts_windows_and_flags_truncated_lower_bound(self) -> None:
        """Count 30/90-day commits and flag the counts as lower bounds when the 100-commit list is truncated."""
        # Arrange
        commits = {"type": "commits", "partial": True, "data": [_iso(2), _iso(20), _iso(60)]}
        releases = {
            "type": "releases",
            "data": [
                {"tag": "v0.9.0", "published": _iso(10)},
                {"tag": "v0.8.0", "published": _iso(40)},
                {"tag": "v0.7.0", "published": _iso(100)},
            ],
        }
        readme = {"type": "readme_content", "data": "# Tool\nThis project is no longer maintained.\n"}

        # Act
        axis = vx.axis2_maintenance(_view(commits, releases, readme))

        # Assert
        assert (axis["commits_30d"], axis["commits_90d"]) == (2, 3)
        assert axis["days_since_last_commit"] == 2.0
        assert axis["commits_30d_is_lower_bound"] is False
        assert axis["commits_90d_is_lower_bound"] is True
        assert axis["release_cadence_days"] == 45.0
        assert axis["latest_release_tag"] == "v0.9.0"
        assert axis["pre_release_tag"] is True
        assert axis["abandonment_keywords"] == ["this project is no longer maintained"]

    def test_axis5_complete_content_decides_every_checkpoint(self) -> None:
        """Decide all five CI checkpoints when every workflow file was read; a missing step is a real miss."""
        # Arrange
        workflows = {
            "type": "ci_workflows",
            "data": {
                "count": 2,
                "names": ["CI", "pages"],
                "workflows": [
                    {"name": "CI", "path": ".github/workflows/ci.yml", "state": "active"},
                    {"name": "pages", "path": "dynamic/pages/pages-build-deployment", "state": "active"},
                ],
            },
        }
        content = {
            "type": "workflow_files",
            "listed": 1,
            "fetched": 1,
            "partial": False,
            "data": "--- workflow: ci.yml ---\nrun: pytest -q\nrun: ruff check\n",
        }
        runs = {
            "type": "ci_runs",
            "data": [{"conclusion": "success"}] * 3 + [{"conclusion": "failure"}, {"conclusion": None}],
        }

        # Act
        axis = vx.axis5_ci(_view(workflows, content, runs))

        # Assert
        assert _states(axis) == {"1": "met", "2": "met", "3": "met", "4": "unmet", "5": "unmet"}
        assert (axis["score_strict"], axis["score_upper"]) == (6, 6)
        assert axis["run_conclusions"] == {"failure": 1, "pending": 1, "success": 3}
        assert axis["ci_pass_rate_pct"] == 75.0

    def test_axis5_unread_files_leave_misses_indeterminate_and_names_never_count(self) -> None:
        """Keep a step not found in partially read content indeterminate; a workflow named CodeQL earns nothing."""
        # Arrange
        workflows = {"type": "ci_workflows", "data": {"count": 3, "names": ["CodeQL", "Lint", "Tests"]}}
        content = {
            "type": "workflow_files",
            "listed": 3,
            "fetched": 1,
            "partial": True,
            "data": "--- workflow: a.yml ---\nrun: pytest\n",
        }
        runs = {"type": "ci_runs", "data": [{"conclusion": "success"}]}

        # Act
        axis = vx.axis5_ci(_view(workflows, content, runs))

        # Assert
        assert _states(axis) == {"1": "met", "2": "met", "3": "indeterminate", "4": "indeterminate", "5": "met"}
        assert (axis["score_strict"], axis["score_upper"]) == (6, 10)
        assert axis["checkpoints"]["4"]["why"] == "security scan: 1 of 3 workflow files read"

    def test_axis5_code_scanning_default_setup_meets_sast(self) -> None:
        """Credit the security-scan checkpoint for active code-scanning default setup, which has no workflow file."""
        # Arrange
        entry = {"name": "CodeQL", "path": "dynamic/github-code-scanning/codeql", "state": "active"}
        workflows = {"type": "ci_workflows", "data": {"count": 1, "names": ["CodeQL"], "workflows": [entry]}}
        content = {"type": "workflow_files", "listed": 1, "fetched": 1, "data": "--- workflow: ci.yml ---\nrun: make\n"}

        # Act
        axis = vx.axis5_ci(_view(workflows, content))

        # Assert
        assert axis["checkpoints"]["4"] == {"state": "met", "why": "code-scanning default setup active"}

    def test_axis5_without_any_ci_data_is_indeterminate(self) -> None:
        """Leave every CI checkpoint indeterminate, not failed, when nothing about workflows was fetched."""
        # Act
        axis = vx.axis5_ci(_view())

        # Assert
        assert set(_states(axis).values()) == {"indeterminate"}
        assert (axis["score_strict"], axis["score_upper"]) == (0, 10)

    @pytest.mark.parametrize(
        ("step", "state"),
        [
            pytest.param("tox -e ci-py$(echo ${{ matrix.python-version }} | tr -d '.') -- -v", "met", id="tox-ci-env"),
            pytest.param("run: tox -e ci-pypy3 -- -v --color=yes", "met", id="tox-ci-pypy-env"),
            pytest.param("run: tox -e py311", "met", id="tox-python-env"),
            pytest.param("run: python -m tox run -e py312", "met", id="tox-run-subcommand"),
            pytest.param("run: tox -e lint,py39", "met", id="tox-env-list-holding-a-test-env"),
            pytest.param("run: nox -s tests-3.11", "met", id="nox-test-session"),
            pytest.param("run: hatch test", "met", id="hatch-test"),
            pytest.param("run: python3 -m unittest discover", "met", id="unittest"),
            pytest.param("run: make test", "met", id="make-test"),
            pytest.param("run: tox -p -e py311", "met", id="tox-flag-before-env"),
            pytest.param("run: tox -vv -e py", "met", id="tox-verbosity-flag-before-env"),
            pytest.param("run: tox run-parallel -e py311", "met", id="tox-run-parallel-subcommand"),
            pytest.param("run: tox p -e py311", "met", id="tox-p-subcommand"),
            pytest.param("run: tox -e py311-django42", "met", id="tox-python-env-with-factors"),
            pytest.param("run: tox -m test", "met", id="tox-test-label"),
            pytest.param("matrix:\n  toxenv: [py39, py310, lint]", "met", id="matrix-toxenv-list-holds-a-test-env"),
            pytest.param("env:\n  TOXENV: py311\nrun: tox", "met", id="toxenv-variable-names-a-test-env"),
            pytest.param("run: npm run test", "met", id="npm-run-test"),
            pytest.param("run: yarn test", "met", id="yarn-test"),
            pytest.param("run: hatch run test:cov", "met", id="hatch-run-test-script"),
            pytest.param("run: tox -c tox.ini -e py311", "met", id="tox-valued-flag-before-env"),
            pytest.param("run: tox --workdir .tox -e py", "met", id="tox-valued-long-flag-before-env"),
            pytest.param("run: tox -e py311-lint,py311", "met", id="tox-env-list-with-a-plain-test-env"),
            pytest.param("run: tox -e py311-types-requests", "met", id="tox-factor-type-is-not-a-prefix-match"),
            pytest.param("run: nox -p 3.11 -s tests", "met", id="nox-valued-flag-before-session"),
            pytest.param("run: pnpm -r test", "met", id="pnpm-flag-before-test"),
            pytest.param("run: pnpm --filter web test", "met", id="pnpm-valued-flag-before-test"),
            pytest.param("run: npm t", "met", id="npm-t-alias"),
            pytest.param("run: tox -f py311", "met", id="tox-factor-filter"),
            pytest.param("run: tox run -f py311 django42", "met", id="tox-run-factor-filter"),
            pytest.param("run: nox -t tests", "met", id="nox-test-tag"),
            pytest.param("run: nox --tags=tests", "met", id="nox-long-test-tag"),
            pytest.param("run: tox -e py311-coverage", "met", id="tox-coverage-factor-still-runs-tests"),
            pytest.param("run: tox -e py311-linters", "unmet", id="tox-linters-factor-is-not-a-test"),
            pytest.param("run: tox -e py312-typecheck", "unmet", id="tox-typecheck-factor-is-not-a-test"),
            pytest.param("run: tox -e py3-pre-commit", "unmet", id="tox-pre-commit-factor-is-not-a-test"),
            pytest.param("run: tox -e py311-coverage-report", "unmet", id="tox-coverage-report-is-not-a-test"),
            pytest.param("run: tox -f lint", "unmet", id="tox-lint-factor-filter-is-not-a-test"),
            pytest.param("run: nox -t lint", "unmet", id="nox-lint-tag-is-not-a-test"),
            pytest.param("run: tox -e py311-lint", "unmet", id="tox-python-lint-env-is-not-a-test"),
            pytest.param("run: tox -e lint-py311", "unmet", id="tox-lint-python-env-is-not-a-test"),
            pytest.param("run: tox -e py39-mypy", "unmet", id="tox-python-mypy-env-is-not-a-test"),
            pytest.param("run: tox -e py312-docs", "unmet", id="tox-python-docs-env-is-not-a-test"),
            pytest.param("run: tox -c tox.ini -e py311-typing", "unmet", id="tox-valued-flag-then-typing-env"),
            pytest.param("env:\n  TOXENV: py311-lint\nrun: tox", "unmet", id="toxenv-variable-names-a-lint-env"),
            pytest.param("run: npm run t", "unmet", id="npm-run-t-is-a-script-named-t"),
            pytest.param("run: tox -e fuzz --result-json out", "unmet", id="tox-fuzz-env-is-not-a-test"),
            pytest.param("run: tox -e run_self", "unmet", id="tox-self-format-env-is-not-a-test"),
            pytest.param("run: tox -e generate_schema", "unmet", id="tox-schema-env-is-not-a-test"),
            pytest.param("run: tox -e pylint", "unmet", id="tox-pylint-env-is-not-a-test"),
            pytest.param("run: tox -e testpypi-upload", "unmet", id="tox-testpypi-publish-env-is-not-a-test"),
            pytest.param("run: tox -e ci-lint", "unmet", id="tox-ci-lint-env-is-not-a-test"),
            pytest.param("run: tox -e py-lint", "unmet", id="tox-py-lint-env-is-not-a-test"),
            pytest.param("run: tox -e ${{ matrix.toxenv }}", "unmet", id="tox-matrix-env-alone-is-undecided"),
            pytest.param("matrix:\n  toxenv: [lint, docs]", "unmet", id="matrix-toxenv-list-without-a-test-env"),
            pytest.param("run: make testpypi", "unmet", id="make-testpypi-is-not-a-test"),
            pytest.param("run: tox", "unmet", id="bare-tox-is-not-credited"),
            pytest.param("hatch:\n  run: python -m hatch build", "unmet", id="hatch-job-key-is-not-a-test"),
            pytest.param("run: nox -s lint", "unmet", id="nox-lint-session-is-not-a-test"),
        ],
    )
    def test_axis5_test_step_shapes(self, step: str, state: str) -> None:
        """Credit tests run through tox, nox, hatch, unittest or make only in a test-shaped invocation.

        psf/black-style CI runs ``tox -e ci-py…`` and never names pytest in a workflow, which scored "no test step";
        crediting bare ``tox`` would instead count lint and fuzz envs. A Python factor beside a lint, docs or typing
        factor credited ``py311-lint`` as a test env, and a flag taking a value (``-c tox.ini``) hid ``-e py311``.
        """
        # Arrange
        content = {"type": "workflow_files", "listed": 1, "fetched": 1, "data": f"--- workflow: t.yml ---\n{step}\n"}

        # Act
        axis = vx.axis5_ci(_view({"type": "workflows_list", "data": ["t.yml"]}, content))

        # Assert
        assert _states(axis)["2"] == state

    @pytest.mark.parametrize(
        ("step", "state"),
        [
            pytest.param("uses: zizmorcore/zizmor-action@cc914d7 # v0.6.4", "met", id="zizmor"),
            pytest.param("uses: google/osv-scanner-action/osv-scanner-action@v2", "met", id="osv-scanner"),
            pytest.param("uses: gitleaks/gitleaks-action@v2", "met", id="gitleaks"),
            pytest.param("run: pip-audit -r requirements.txt", "met", id="pip-audit"),
            pytest.param("uses: trufflesecurity/trufflehog@v3", "met", id="trufflehog"),
            pytest.param("uses: actions/dependency-review-action@v4", "met", id="dependency-review"),
            pytest.param("uses: ossf/scorecard-action@v2", "met", id="openssf-scorecard"),
            pytest.param("run: make check", "unmet", id="no-scanner"),
        ],
    )
    def test_axis5_security_scanners(self, step: str, state: str) -> None:
        """Credit Actions-workflow, dependency and secret scanners as the security-scan checkpoint."""
        # Arrange
        content = {"type": "workflow_files", "listed": 1, "fetched": 1, "data": f"--- workflow: s.yml ---\n{step}\n"}

        # Act
        axis = vx.axis5_ci(_view({"type": "workflows_list", "data": ["s.yml"]}, content))

        # Assert
        assert _states(axis)["4"] == state

    @pytest.mark.parametrize(
        ("conclusions", "state", "sampled", "pass_rate"),
        [
            pytest.param(
                ["success", "skipped", "failure"]
                + ["success"] * 5
                + ["failure", "cancelled", "cancelled"]
                + ["success"] * 4
                + ["skipped"]
                + ["success"] * 5,
                "met",
                17,
                88.2,
                id="black-shaped-skipped-and-cancelled-excluded",
            ),
            pytest.param(
                [None] * 3 + ["success"] * 15 + ["failure"] * 3,
                "met",
                18,
                83.3,
                id="pending-at-head-excluded-before-the-slice",
            ),
            pytest.param(["success"] * 16 + ["failure"] * 4 + ["failure"] * 5, "met", 20, 80.0, id="newest-20-only"),
            pytest.param(
                ["success"] * 7 + ["action_required", "timed_out", "startup_failure"],
                "unmet",
                10,
                70.0,
                id="auth-timeout-and-unknown-outcomes-count-as-failures",
            ),
            pytest.param(["skipped", "neutral", "cancelled"], "indeterminate", 0, None, id="nothing-counted"),
        ],
    )
    def test_axis5_run_sample(
        self, conclusions: list[str | None], state: str, sampled: int, pass_rate: float | None
    ) -> None:
        """Count the newest 20 runs that executed and concluded; skipped, neutral and cancelled runs stay out.

        Slicing before excluding left 17 of 20 slots when 3 runs were pending, and counting skipped conditional
        workflows and superseded cancellations as failures put psf/black at 70% instead of 88%. The printed pass rate is
        rounded; the 80% threshold reads the counts.
        """
        # Arrange
        runs = {"type": "ci_runs", "data": [{"conclusion": conclusion} for conclusion in conclusions]}

        # Act
        axis = vx.axis5_ci(_view({"type": "workflows_list", "data": ["ci.yml"]}, runs))

        # Assert
        printed = vx._display(axis)
        assert (_states(axis)["5"], axis["runs_sampled"], printed["ci_pass_rate_pct"]) == (state, sampled, pass_rate)

    @pytest.mark.parametrize(
        ("runs", "state", "sampled", "excluded"),
        [
            pytest.param(
                [{"conclusion": "failure", "event": "pull_request"}] * 5
                + [{"conclusion": "success", "event": "push"}] * 20,
                "met",
                20,
                5,
                id="fork-pr-failures-at-the-head-never-enter",
            ),
            pytest.param(
                [{"conclusion": "failure", "event": "pull_request_target"}] * 3
                + [{"conclusion": "success", "event": "schedule"}] * 4
                + [{"conclusion": "failure", "event": "push"}],
                "met",
                5,
                3,
                id="scheduled-runs-count-pr-target-does-not",
            ),
            pytest.param(
                [{"conclusion": "failure", "event": "pull_request"}] * 2,
                "indeterminate",
                0,
                2,
                id="only-pr-runs-leave-the-sample-empty",
            ),
            pytest.param(
                [{"conclusion": "success", "event": "workflow_run"}] * 8
                + [{"conclusion": "success", "event": "dynamic"}] * 2
                + [{"conclusion": "failure", "event": "push"}] * 3
                + [{"conclusion": "success", "event": "push"}] * 7,
                "unmet",
                10,
                10,
                id="pr-comment-bots-and-dynamic-runs-never-pad-the-sample",
            ),
            pytest.param(
                [{"conclusion": "success", "event": "workflow_dispatch"}] * 2
                + [{"conclusion": "success", "event": "merge_group"}] * 2
                + [{"conclusion": "failure", "event": "push"}],
                "met",
                5,
                0,
                id="dispatch-and-merge-queue-runs-count",
            ),
        ],
    )
    def test_axis5_run_sample_counts_only_ci_events(
        self, runs: list[dict], state: str, sampled: int, excluded: int
    ) -> None:
        """Sample only push, schedule, workflow_dispatch and merge_group runs — the default branch's own CI.

        ``branch=<default>`` matches a run's head branch, so a pull request opened from a fork's own ``main`` comes back
        in the default-branch fetch; counting its work-in-progress failures pushed a green project below 80%.
        psf/black's newest 20 held eight ``workflow_run`` PR-comment bot runs and two ``dynamic`` Dependabot runs, which
        almost always succeed and measured bots instead of the project.
        """
        # Arrange
        record = {"type": "ci_runs", "branch": "main", "data": runs}
        status = {"type": "default_branch_status", "branch": "main", "data": {"protected": True}}

        # Act
        axis = vx.axis5_ci(_view({"type": "workflows_list", "data": ["ci.yml"]}, record, status))

        # Assert
        assert (_states(axis)["5"], axis["runs_sampled"], axis["runs_event_excluded"]) == (state, sampled, excluded)
        assert axis["runs_scope"] == "default branch main, push/schedule/workflow_dispatch/merge_group events only"

    def test_axis5_reports_the_page_the_sample_came_from(self) -> None:
        """Report the fetched run count beside the sample, so a short sample from a full page reaches the notes.

        One page of 100 default-branch runs holds the newest 20 counted runs only while at most 80 are excluded;
        psf/black's page already held 48 excluded runs. Past that, older counted runs sit on the next page while only a
        sample below 10 degrades confidence, so the page size must be visible next to ``runs_sampled``.
        """
        # Arrange
        runs = [{"conclusion": "success", "event": "workflow_run"}] * 85 + [
            {"conclusion": "success", "event": "push"}
        ] * 15
        record = {"type": "ci_runs", "branch": "main", "data": runs}
        status = {"type": "default_branch_status", "branch": "main", "data": {"protected": True}}

        # Act
        axis = vx.axis5_ci(_view({"type": "workflows_list", "data": ["ci.yml"]}, record, status))

        # Assert
        assert (axis["runs_fetched"], axis["runs_event_excluded"], axis["runs_sampled"]) == (100, 85, 15)

    @pytest.mark.parametrize(
        ("records", "state"),
        [
            pytest.param(
                [{"type": "ci_runs", "branch": "main", "data": []}], "indeterminate", id="guessed-branch-without-runs"
            ),
            pytest.param(
                [
                    {"type": "ci_runs", "branch": "main", "data": []},
                    {"type": "default_branch_status", "branch": "main", "data": {"protected": False}},
                ],
                "unmet",
                id="confirmed-branch-without-runs",
            ),
            pytest.param([{"type": "ci_runs", "data": []}], "unmet", id="all-branch-record-without-runs"),
        ],
    )
    def test_axis5_empty_run_list_needs_a_confirmed_branch(self, records: list[dict], state: str) -> None:
        """Read "no run" as unmet only on a branch the data confirmed; a guessed branch leaves checkpoint 5 undecided.

        oss:gh-scraper falls back to ``main`` when ``repo_metadata`` is missing; on a ``master`` repository the branch-
        scoped fetch returns no run, and ``default_branch_status`` for that branch is missing too.
        """
        # Act
        axis = vx.axis5_ci(_view({"type": "workflows_list", "data": ["ci.yml"]}, *records))

        # Assert
        assert _states(axis)["5"] == state

    def test_axis6_complete_data_decides_every_checkpoint(self) -> None:
        """Decide the nine documentation checkpoints from README, listings, changelog headings and CONTRIBUTING."""
        # Arrange
        records = (
            {"type": "readme_content", "data": "## Install\npip install x\n" + "x" * 600},
            {"type": "root_contents", "data": ["README.md", "CHANGELOG.md", "docs", ".github"]},
            {"type": "github_dir", "data": ["CONTRIBUTING.md"]},
            {
                "type": "changelog_headings",
                "source": "CHANGELOG.md",
                "data": {"head": [], "headings": [f"## [1.0] - {_iso(10)[:10]}"]},
            },
            {
                "type": "contributing_text",
                "source": ".github/CONTRIBUTING.md",
                "data": "Dev setup: make env\nOpen a pull request\n",
            },
        )

        # Act
        axis = vx.axis6_docs(_view(*records))

        # Assert
        expected = {
            "1": "met",
            "2": "met",
            "3": "unmet",
            "4": "met",
            "5": "met",
            "6": "unmet",
            "7": "met",
            "8": "met",
            "9": "unmet",
        }
        assert _states(axis) == expected
        assert (axis["met"], axis["indeterminate"], axis["score_strict"], axis["score_upper"]) == (6, 0, 6, 6)
        assert axis["contributing_source"] == ".github/CONTRIBUTING.md"

    @pytest.mark.parametrize(
        ("records", "state", "why"),
        [
            pytest.param(
                [
                    {
                        "type": "changelog_headings",
                        "source": "CHANGELOG.md",
                        "data": {"head": [], "headings": ["## 1.0 - 2020-01-02"]},
                    }
                ],
                "unmet",
                "CHANGELOG.md: newest dated entry",
                id="old-entry",
            ),
            pytest.param(
                [{"type": "changelog_headings", "source": "NEWS", "data": {"head": ["News"], "headings": ["## 1.0"]}}],
                "indeterminate",
                "NEWS: no dated heading found",
                id="no-date",
            ),
            pytest.param(
                [{"type": "root_contents", "data": ["CHANGELOG.md"]}],
                "indeterminate",
                "changelog listed",
                id="not-fetched",
            ),
            pytest.param([{"type": "root_contents", "data": ["README.md"]}], "unmet", "no changelog file", id="absent"),
            pytest.param(
                [{"type": "root_contents", "data": ["README.md", "changes"]}],
                "indeterminate",
                "changelog-like entry listed",
                id="bare-name-may-be-a-directory",
            ),
            pytest.param(
                [
                    {
                        "type": "changelog_headings",
                        "source": "CHANGELOG.md",
                        "truncated": True,
                        "data": {"head": [], "headings": ["## 0.1 - 2019-01-02"]},
                    }
                ],
                "indeterminate",
                "CHANGELOG.md: heading list truncated",
                id="truncated-oldest-first-outline",
            ),
            pytest.param(
                [
                    {
                        "type": "changelog_headings",
                        "source": "CHANGES.md",
                        "truncated": True,
                        "data": {"head": ["# Change Log"], "headings": ["## Unreleased", "## 25.9.0", "## 25.1.0"]},
                    }
                ],
                "indeterminate",
                "CHANGES.md: no dated heading found (heading list truncated)",
                id="undated-truncated-outline-names-the-undated-cause",
            ),
            pytest.param(
                [
                    {
                        "type": "changelog_headings",
                        "source": "CHANGES.md",
                        "truncated": True,
                        "data": {
                            "head": ["# Change Log"],
                            "headings": ["## Unreleased", "### Highlights", "## Version 26.10.0", "## Version 26.5.1"],
                        },
                    },
                    {
                        "type": "releases",
                        "data": [
                            {"tag": "26.10.0", "published": _iso(4)},
                            {"tag": "26.5.1", "published": _iso(143)},
                        ],
                    },
                ],
                "met",
                "CHANGES.md: version heading 26.10.0 matches a release published 4d ago",
                id="black-shaped-version-heading-dated-by-its-release",
            ),
            pytest.param(
                [
                    {
                        "type": "changelog_headings",
                        "source": "CHANGES.md",
                        "truncated": True,
                        "data": {"head": ["# Change Log"], "headings": ["## Unreleased", "## Version 24.1.0"]},
                    },
                    {"type": "releases", "data": [{"tag": "24.1.0", "published": _iso(400)}]},
                ],
                "indeterminate",
                "CHANGES.md: no dated heading found (heading list truncated)",
                id="version-heading-of-an-old-release-stays-undated",
            ),
            pytest.param(
                [
                    {
                        "type": "changelog_headings",
                        "source": "CHANGELOG.md",
                        "data": {"head": [], "headings": ["## [1.1.0]", "## [1.0.0] - 2020-01-02"]},
                    },
                    {"type": "releases", "data": [{"tag": "v1.1.0", "published": _iso(30)}]},
                ],
                "met",
                "CHANGELOG.md: version heading 1.1.0 matches a release published 30d ago",
                id="v-prefixed-tag-dates-a-heading-newer-than-the-old-dated-one",
            ),
            pytest.param(
                [
                    {
                        "type": "changelog_headings",
                        "source": "CHANGES.md",
                        "data": {"head": [], "headings": ["## Unreleased", "## Version 26.10.0"]},
                    },
                    {"type": "releases", "data": [{"tag": "26.9.0", "published": _iso(4)}]},
                ],
                "indeterminate",
                "CHANGES.md: no dated heading found",
                id="release-without-a-matching-heading-dates-nothing",
            ),
        ],
    )
    def test_axis6_changelog_checkpoint(self, records: list[dict], state: str, why: str) -> None:
        """Date the changelog from its newest dated heading or a release its version heading names; else unknown.

        psf/black's ``CHANGES.md`` heads each entry ``## Version 26.10.0`` without a date, so checkpoint 4 stayed
        indeterminate while the release of that version was four days old.
        """
        # Act
        checkpoint = vx.axis6_docs(_view(*records))["checkpoints"]["4"]

        # Assert
        assert checkpoint["state"] == state
        assert checkpoint["why"].startswith(why)

    @pytest.mark.parametrize(
        ("records", "state"),
        [
            pytest.param(
                [{"type": "github_dir", "data": ["CONTRIBUTING.md"]}], "indeterminate", id="listed-not-fetched"
            ),
            pytest.param(
                [
                    {"type": "root_contents", "data": ["README.md", ".github"]},
                    {"type": "github_dir", "data": ["workflows"]},
                ],
                "unmet",
                id="confirmed-absent",
            ),
        ],
    )
    def test_axis6_contributing_checkpoints_without_text(self, records: list[dict], state: str) -> None:
        """Mark CONTRIBUTING content checkpoints unmet only when both listings prove the file is absent."""
        # Act
        axis = vx.axis6_docs(_view(*records))

        # Assert
        assert {_states(axis)[n] for n in ("7", "8", "9")} == {state}

    @pytest.mark.parametrize(
        ("docs_dir", "cp6", "cp7"),
        [
            pytest.param(None, "indeterminate", "indeterminate", id="docs-listing-unknown-is-undecidable"),
            pytest.param([], "unmet", "unmet", id="docs-listing-without-file-is-absent"),
            pytest.param(["CONTRIBUTING.rst"], "indeterminate", "met", id="docs-rst-listed-not-fetched"),
        ],
    )
    def test_contributing_kept_only_in_docs(self, docs_dir: list[str] | None, cp6: str, cp7: str) -> None:
        """Read CONTRIBUTING in ``docs/`` as unproven until the ``docs/`` listing is known, in any extension.

        A failed ``docs/CONTRIBUTING.md`` fetch used to leave the file "absent": checkpoints 7–9 unmet and Axis 7
        checkpoint 4 unmet at full confidence, and ``docs/CONTRIBUTING.rst`` was never considered.
        """
        # Arrange
        records = [
            {"type": "root_contents", "data": ["README.md", "docs", ".github"]},
            {"type": "github_dir", "data": ["workflows"]},
        ]
        records += [] if docs_dir is None else [{"type": "docs_dir", "data": docs_dir}]
        view = _view(*records)

        # Act
        states = (_states(vx.axis6_docs(view))["7"], _states(vx.axis7_governance(view))["4"])

        # Assert
        assert states == (cp6, cp7)


class TestAxis6ThresholdBoundaries:
    """Axis 6 checkpoint thresholds: a value on the threshold meets the checkpoint, like every 🟢 clause."""

    @pytest.mark.parametrize(
        ("size", "state"),
        [pytest.param(500, "met", id="500-bytes-meets"), pytest.param(499, "unmet", id="499-bytes-misses")],
    )
    def test_readme_size(self, size: int, state: str) -> None:
        """Meet checkpoint 1 with a README of exactly 500 bytes; the threshold belongs to the better outcome."""
        # Act
        axis = vx.axis6_docs(_view({"type": "readme_content", "data": "x" * size}))

        # Assert
        assert axis["checkpoints"]["1"]["state"] == state

    @pytest.mark.parametrize(
        ("age_days", "hour", "state"),
        [
            pytest.param(365, 0, "met", id="entry-365d-old-at-midnight-meets"),
            pytest.param(365, 13, "met", id="entry-365d-old-in-the-afternoon-meets"),
            pytest.param(365, 23.99, "met", id="entry-365d-old-before-midnight-meets"),
            pytest.param(366, 0, "unmet", id="entry-366d-old-at-midnight-misses"),
            pytest.param(366, 13, "unmet", id="entry-366d-old-in-the-afternoon-misses"),
        ],
    )
    def test_changelog_age(self, age_days: int, hour: float, state: str) -> None:
        """Meet checkpoint 4 with a newest changelog entry exactly 365 calendar days old, at any time of day.

        Changelog dates carry no time, so measuring in seconds made a 365-day-old entry 365.5 days old at a 13:00 scrape
        and stale at every instant real runs hit; the test used to pin only midnight.
        """
        # Arrange
        now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() + age_days * _DAY + hour * 3600
        record = {
            "source": "CHANGELOG.md",
            "truncated": False,
            "data": {"head": [], "headings": ["## 1.0 - 2026-01-01"]},
        }

        # Act
        checkpoint = vx._changelog_record_checkpoint(record, now)

        # Assert
        assert checkpoint["state"] == state


class TestGroupB:
    """Axes 4, 7 and 8."""

    @pytest.mark.parametrize(
        ("records", "state"),
        [
            pytest.param(
                [
                    {"type": "root_contents", "data": ["docs", "src"]},
                    {"type": "github_dir", "data": ["workflows"]},
                    {"type": "docs_dir", "data": ["index.md", "CODEOWNERS"]},
                ],
                "met",
                id="codeowners-listed-in-docs",
            ),
            pytest.param(
                [{"type": "root_contents", "data": ["docs", "src"]}, {"type": "github_dir", "data": ["workflows"]}],
                "indeterminate",
                id="docs-listing-unknown",
            ),
            pytest.param(
                [{"type": "root_contents", "data": ["src"]}, {"type": "github_dir", "data": ["workflows"]}],
                "unmet",
                id="no-codeowners-anywhere",
            ),
        ],
    )
    def test_axis7_codeowners_in_docs(self, records: list[dict], state: str) -> None:
        """Read CODEOWNERS from ``docs/`` too; with ``docs/`` listed but unread, absence is undecided, never unmet."""
        # Act
        axis = vx.axis7_governance(_view(*records))

        # Assert
        assert axis["checkpoints"]["5"]["state"] == state

    def test_axis4_issue_and_pr_rates(self) -> None:
        """Compute stale share, 30-day close rate, abandoned share and 30-day merge rate."""
        # Arrange
        records = (
            {
                "type": "open_issues",
                "data": [
                    {"createdAt": _iso(200), "updatedAt": _iso(100)},
                    {"createdAt": _iso(5), "updatedAt": _iso(5)},
                ],
            },
            {"type": "closed_issues", "data": [{"createdAt": _iso(10), "closedAt": _iso(3)}]},
            {"type": "open_prs", "data": [{"createdAt": _iso(40), "updatedAt": _iso(35)}]},
            {
                "type": "closed_prs",
                "data": [
                    {"createdAt": _iso(9), "closedAt": _iso(2), "mergedAt": _iso(2)},
                    {"createdAt": _iso(8), "closedAt": _iso(1), "mergedAt": None},
                ],
            },
        )

        # Act
        axis = vx.axis4_issue_pr(_view(*records))

        # Assert
        assert (axis["issues"]["stale_pct"], axis["issues"]["opened_30d"], axis["issues"]["close_rate"]) == (
            50.0,
            2,
            0.5,
        )
        assert (axis["prs"]["abandoned_pct"], axis["prs"]["merged_30d"], axis["prs"]["merge_rate"]) == (100.0, 1, 0.5)
        assert axis["prs"]["closed_without_merge_ratio"] == 0.5

    @pytest.mark.parametrize(
        ("records", "close_rate", "extra"),
        [
            pytest.param(
                [{"type": "open_issues", "data": [{"createdAt": _iso(5), "updatedAt": _iso(5)}]}],
                None,
                {"closed_available": False, "closed_30d": None},
                id="closed-list-missing",
            ),
            pytest.param(
                [
                    {"type": "open_issues", "data": [{"createdAt": _iso(50), "updatedAt": _iso(5)}]},
                    {"type": "closed_issues", "data": []},
                ],
                None,
                {"rate_null_reason": "no issues opened in 30d"},
                id="empty-30d-window",
            ),
        ],
    )
    def test_axis4_close_rate_is_null_without_a_denominator(
        self, records: list[dict], close_rate: float | None, extra: dict
    ) -> None:
        """Leave the close rate null when its list is missing or nothing opened in 30 days, never a 🔴-forcing 0."""
        # Act
        issues = vx.axis4_issue_pr(_view(*records))["issues"]

        # Assert
        assert issues["close_rate"] is close_rate
        assert {key: issues[key] for key in extra} == extra

    @pytest.mark.parametrize(
        ("closed_ages", "partial", "lower_bound"),
        [
            pytest.param([1, 20], True, True, id="truncated-inside-window"),
            pytest.param([1, 40], True, True, id="truncated-beyond-window-still-unordered"),
            pytest.param([1, 20], False, False, id="complete-list"),
        ],
    )
    def test_axis4_closed_list_lower_bound(self, closed_ages: list[int], partial: bool, lower_bound: bool) -> None:
        """Flag 30-day PR counts as lower bounds whenever the closed list is truncated.

        The list is not ordered by closing date: a creation-ordered list reaching 103 days back still missed PRs
        created earlier and merged this month, while the old "oldest entry inside the window" test called it complete.
        """
        # Arrange
        closed = [{"createdAt": _iso(age + 1), "closedAt": _iso(age), "mergedAt": _iso(age)} for age in closed_ages]
        records = ({"type": "open_prs", "data": []}, {"type": "closed_prs", "partial": partial, "data": closed})

        # Act
        prs = vx.axis4_issue_pr(_view(*records))["prs"]

        # Assert
        assert prs["closed_30d_is_lower_bound"] is lower_bound

    @pytest.mark.parametrize(
        ("count", "partial", "lower_bound"),
        [
            pytest.param(1000, False, True, id="search-ceiling-reached-in-older-data"),
            pytest.param(1000, True, True, id="search-ceiling-flagged-by-the-assembler"),
            pytest.param(999, False, False, id="below-the-search-ceiling"),
        ],
    )
    def test_axis4_closed_issues_at_the_search_ceiling_are_a_lower_bound(
        self, count: int, partial: bool, lower_bound: bool
    ) -> None:
        """Read a closed-issue list of 1000 as truncated: GitHub search never returns more, whatever the fetch limit.

        The fetch asked for 1001 to prove truncation, but ``gh issue list --search`` stops at 1000, so a month with more
        closures read as complete and the close rate lost its lower-bound degrader.
        """
        # Arrange
        closed = [{"createdAt": _iso(40), "closedAt": _iso(5)}] * count
        records = ({"type": "open_issues", "data": []}, {"type": "closed_issues", "partial": partial, "data": closed})

        # Act
        issues = vx.axis4_issue_pr(_view(*records))["issues"]

        # Assert
        assert (issues["closed_30d"], issues["closed_30d_is_lower_bound"]) == (count, lower_bound)

    def test_axis7_accepts_hyphenated_code_of_conduct_and_strips_codeowners_comments(self) -> None:
        """Meet checkpoint 3 with ``CODE-OF-CONDUCT.md`` and never read an owner out of a trailing comment."""
        # Arrange
        records = (
            {"type": "root_contents", "data": ["CODE-OF-CONDUCT.md"]},
            {"type": "github_dir", "data": []},
            {"type": "codeowners_text", "data": "* @alice  # backup: @bob\n"},
        )

        # Act
        axis = vx.axis7_governance(_view(*records))

        # Assert
        assert axis["checkpoints"]["3"]["state"] == "met"
        assert axis["codeowners_users"] == ["alice"]

    @pytest.mark.parametrize("name", ["dependabot.yml", "dependabot.yaml"])
    def test_axis8_dependabot_config_either_extension(self, name: str) -> None:
        """Credit a Dependabot config listed under either extension GitHub accepts."""
        # Arrange
        records = ({"type": "root_contents", "data": [".github"]}, {"type": "github_dir", "data": [name]})

        # Act
        checkpoint = vx.axis8_security(_view(*records))["secondary"]["dep_config"]

        # Assert
        assert checkpoint["state"] == "met"

    def test_axis4_pr_rates_exclude_bot_authors(self) -> None:
        """Drop bot-authored PRs from the PR queue metrics when the lists carry authors."""
        # Arrange
        bot = {"login": "app/pre-commit-ci", "is_bot": True}
        human = {"login": "alice", "is_bot": False}
        records = (
            {"type": "open_prs", "data": [{"author": bot, "createdAt": _iso(2), "updatedAt": _iso(2)}]},
            {
                "type": "closed_prs",
                "data": [
                    {"author": human, "createdAt": _iso(9), "closedAt": _iso(2), "mergedAt": _iso(2)},
                    {"author": bot, "createdAt": _iso(3), "closedAt": _iso(1), "mergedAt": _iso(1)},
                ],
            },
        )

        # Act
        prs = vx.axis4_issue_pr(_view(*records))["prs"]

        # Assert
        assert (prs["bot_filter"], prs["bots_excluded"]) == ("applied", 2)
        assert (prs["open_count"], prs["opened_30d"], prs["merged_30d"], prs["merge_rate"]) == (0, 1, 1, 1.0)

    def test_axis4_review_coverage_ignores_bots_and_self_approval(self) -> None:
        """Count only non-bot PRs approved by someone other than the author; flag samples under five as undefined."""
        # Arrange
        nodes = [
            {"author": {"login": "a"}, "reviews": {"nodes": [{"author": {"login": "m"}}]}},
            {"author": {"login": "b"}, "reviews": {"nodes": [{"author": {"login": "b"}}]}},
            {"author": {"login": "dependabot[bot]"}, "reviews": {"nodes": [{"author": {"login": "m"}}]}},
        ]
        record = {"type": "review_coverage_gql", "data": {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}}

        # Act
        coverage = vx.axis4_issue_pr(_view(record))["review_coverage"]

        # Assert
        assert (coverage["non_bot"], coverage["approved_by_other"], coverage["coverage_pct"]) == (2, 1, 50.0)
        assert coverage["undefined"] is True

    def test_axis7_governance_checkpoints_and_active_maintainers(self) -> None:
        """Decide the seven governance checkpoints, including the active share of CODEOWNERS users."""
        # Arrange
        records = (
            {"type": "root_contents", "data": ["LICENSE", "SECURITY.md", ".github"]},
            {"type": "github_dir", "data": ["CODEOWNERS", "CONTRIBUTING.md"]},
            {"type": "codeowners_text", "data": "# owners\n* @Alice @org/team\n/docs @bob\n"},
            {
                "type": "contributor_stats",
                "data": [
                    {"author": "alice", "weeks": _weeks(*[0] * 12, 1)},
                    {"author": "bob", "weeks": _weeks(1, *[0] * 13)},
                ],
            },
            {"type": "default_branch_status", "data": {"protected": True}},
        )

        # Act
        axis = vx.axis7_governance(_view(*records))

        # Assert
        assert _states(axis) == {"1": "met", "2": "met", "3": "unmet", "4": "met", "5": "met", "6": "met", "7": "met"}
        assert (axis["met"], axis["applicable"], axis["score_strict"]) == (6, 7, 8)
        assert axis["codeowners_users"] == ["alice", "bob"]
        assert (axis["active_maintainers"], axis["listed_maintainers"], axis["active_ratio"]) == (1, 2, 0.5)

    @pytest.mark.parametrize(
        ("records", "state"),
        [
            pytest.param([{"type": "default_branch_status", "data": {"protected": False}}], "unmet", id="unprotected"),
            pytest.param([{"type": "branch_protection", "data": {}}], "met", id="admin-rules-readable"),
            pytest.param([], "indeterminate", id="flag-not-fetched"),
        ],
    )
    def test_axis7_branch_protection_checkpoint(self, records: list[dict], state: str) -> None:
        """Decide branch protection from the public protected flag, else the admin-only rules record."""
        # Act
        checkpoint = vx.axis7_governance(_view(*records))["checkpoints"]["6"]

        # Assert
        assert checkpoint["state"] == state

    def test_axis7_checkpoint7_not_applicable_while_stats_pending(self) -> None:
        """Drop the active-maintainer checkpoint from the denominator while contributor stats are computing (202)."""
        # Arrange
        records = (
            {"type": "codeowners_text", "data": "* @alice\n"},
            {"type": "contributor_stats", "data": None, "partial": True, "202_pending": True},
        )

        # Act
        axis = vx.axis7_governance(_view(*records))

        # Assert
        assert axis["checkpoints"]["7"] == {"state": "not_applicable", "why": "contributor stats 202_pending"}
        assert axis["applicable"] == 6
        assert axis["active_ratio"] is None

    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            pytest.param(None, {"status": "absent"}, id="absent"),
            pytest.param({"type": "dependabot_alerts", "data": "403"}, {"status": "403"}, id="forbidden"),
            pytest.param(
                {
                    "type": "dependabot_alerts",
                    "data": [
                        {"security_advisory": {"severity": "high"}},
                        {"security_vulnerability": {"severity": "critical"}},
                    ],
                },
                {"status": "available", "open_alerts": 2, "by_severity": {"critical": 1, "high": 1}, "at_limit": False},
                id="available-by-severity",
            ),
        ],
    )
    def test_axis8_dependabot_status(self, record: dict | None, expected: dict) -> None:
        """Separate unavailable (403), absent and available alert data so partial scoring triggers correctly."""
        # Arrange
        view = _view(record) if record else _view()

        # Act
        axis = vx.axis8_security(view)

        # Assert
        assert axis["dependabot"] == expected

    def test_axis8_secondary_signals_and_partial_score(self) -> None:
        """Decide config, dependency-commit and SECURITY depth signals and the 403 partial score from them."""
        # Arrange
        records = (
            {"type": "root_contents", "data": ["renovate.json"]},
            {
                "type": "commits_50",
                "data": [
                    {"message": "chore(deps): bump x", "date": _iso(5)},
                    {"message": "Bump y", "date": _iso(120)},
                    {"message": "fix", "date": _iso(1)},
                ],
            },
            {"type": "security_text", "data": "Email security@example.org; we reply within 48 hours.\n"},
        )

        # Act
        axis = vx.axis8_security(_view(*records))

        # Assert
        assert {key: cp["state"] for key, cp in axis["secondary"].items()} == {
            "dep_config": "met",
            "dep_update_commits": "met",
            "security_md": "met",
        }
        assert axis["secondary"]["dep_update_commits"]["why"].startswith("1 dependency-update commits")
        assert axis["security_md_depth"] == 3
        assert (axis["partial_score_strict"], axis["partial_score_upper"]) == (10, 10)

    def test_axis8_unknown_listings_lower_only_the_strict_score(self) -> None:
        """Score unconfirmable secondary signals as absent while the upper bound credits them."""
        # Arrange
        records = ({"type": "commits_50", "data": []},)

        # Act
        axis = vx.axis8_security(_view(*records))

        # Assert
        assert axis["secondary"]["dep_config"]["state"] == "indeterminate"
        assert axis["secondary"]["security_md"]["state"] == "indeterminate"
        assert (axis["partial_score_strict"], axis["partial_score_upper"]) == (0, 6)


class TestGroupC:
    """Axes 3 and 9."""

    def test_axis3_bus_factor_retention_and_weeks_summary(self) -> None:
        """Derive bus factor, top share and retention from the last 13 weeks, excluding bots."""
        # Arrange
        stats = [
            {"author": "alice", "weeks": _weeks(*[0] * 40, 3, *[0] * 5, 3, *[0] * 6)},
            {"author": "bob", "weeks": _weeks(*[0] * 40, 1, *[0] * 12)},
            {"author": "renovate[bot]", "weeks": _weeks(*[0] * 52, 9)},
        ]

        # Act
        axis = vx.axis3_contributors(_view({"type": "contributor_stats", "data": stats}))

        # Assert
        assert (axis["contributors"], axis["commits_90d_total"], axis["bus_factor"]) == (2, 7, 1)
        assert (axis["top_contributor"], vx._display(axis)["top_contributor_pct"]) == ("alice", 85.7)
        assert (axis["retention_active_q1"], axis["retention_active_both"], axis["retention_pct"]) == (2, 1, 50.0)
        assert axis["axis3_weeks"] == {
            "contributors": 2,
            "pool_recent_26w": 2,
            "pool_prior_26w": 0,
            "q1_active": 2,
            "q1q2_active": 1,
        }

    def test_axis3_pending_stats_return_fallback_only(self) -> None:
        """Expose the commit-author fallback and a null weeks summary while stats are pending."""
        # Arrange
        records = (
            {"type": "contributor_stats", "data": None, "partial": True, "202_pending": True},
            {"type": "commits_50", "data": [{"author": "alice"}, {"author": "bob"}, {"author": "dependabot[bot]"}]},
        )

        # Act
        axis = vx.axis3_contributors(_view(*records))

        # Assert
        assert axis["stats_status"] == "202_pending"
        assert axis["axis3_weeks"] is None
        assert axis["fallback"]["unique_non_bot_authors"] == 2
        assert axis["fallback"]["approx_bus_factor"] == 2

    def test_axis3_fallback_caps_at_three_and_skips_unresolved_authors(self) -> None:
        """Approximate the bus factor as distinct non-bot authors capped at 3; ``unknown`` may be several people."""
        # Arrange
        authors = ["a", "b", "c", "d", "unknown", "unknown", "renovate[bot]"]
        records = (
            {"type": "contributor_stats", "data": None, "202_pending": True},
            {"type": "commits_50", "data": [{"author": name} for name in authors]},
        )

        # Act
        fallback = vx.axis3_contributors(_view(*records))["fallback"]

        # Assert
        assert fallback == {"commits_sampled": 7, "unique_non_bot_authors": 4, "approx_bus_factor": 3}

    def test_axis3_skips_contributors_without_an_author(self) -> None:
        """Skip null-author stats entries instead of merging every deleted account into one contributor."""
        # Arrange
        stats = [
            {"author": None, "weeks": _weeks(*[0] * 40, 5, *[0] * 12)},
            {"author": None, "weeks": _weeks(*[0] * 40, 5, *[0] * 12)},
            {"author": "alice", "weeks": _weeks(*[0] * 40, 1, *[0] * 12)},
        ]

        # Act
        axis = vx.axis3_contributors(_view({"type": "contributor_stats", "data": stats}))

        # Assert
        assert (axis["contributors"], axis["commits_90d_total"], axis["bus_factor"]) == (1, 1, 1)

    def test_axis9_trend_ratio_from_unrounded_medians(self) -> None:
        """Keep a merge-time trend for PRs merged within minutes, where rounded medians were both 0.00."""
        # Arrange
        merged = [
            {"author": {"login": "a"}, "createdAt": _iso(days + 0.004), "mergedAt": _iso(days)} for days in (1, 2, 40)
        ]

        # Act
        trend = vx.axis9_trajectory(_view({"type": "merged_prs_90d", "data": merged}))["9B_merge_trend"]

        # Assert
        printed = vx._display(trend)
        assert (printed["median_30d_days"], printed["median_90d_days"]) == (0.0, 0.0)
        assert trend["trend_ratio"] == 1.0

    def test_axis9_sub_signals(self) -> None:
        """Compute merge trend over non-bot PRs, P90 open-issue age and the automated-commit ratio."""
        # Arrange
        records = (
            {"type": "contributor_stats", "data": None, "202_pending": True},
            {
                "type": "merged_prs_90d",
                "partial": True,
                "data": [
                    {"author": {"login": "a", "is_bot": False}, "createdAt": _iso(12), "mergedAt": _iso(10)},
                    {"author": {"login": "b", "is_bot": False}, "createdAt": _iso(61), "mergedAt": _iso(60)},
                    {
                        "author": {"login": "app/pre-commit-ci", "is_bot": True},
                        "createdAt": _iso(3),
                        "mergedAt": _iso(2),
                    },
                ],
            },
            {"type": "open_issues", "data": [{"createdAt": _iso(days)} for days in (1, 2, 3, 4, 5, 6, 7, 8, 9, 100)]},
            {
                "type": "commits_50",
                "data": [{"message": "Bump x", "author": "dependabot[bot]"}, {"message": "Bump y", "author": "alice"}],
            },
        )

        # Act
        axis = vx._display(vx.axis9_trajectory(_view(*records)))

        # Assert
        assert axis["9A_pool_drift"] == {"available": False, "reason": "contributor stats 202_pending"}
        trend = axis["9B_merge_trend"]
        assert (trend["merged_non_bot"], trend["bots_excluded"], trend["merged_30d"]) == (2, 1, 1)
        assert (trend["median_30d_days"], trend["median_90d_days"], trend["trend_ratio"]) == (2.0, 1.5, 1.33)
        assert trend["window_days_covered"] == 60.0
        assert axis["9C_queue_depth"]["p90_age_days"] == 100.0
        assert axis["9D_automation"] == {"commits_sampled": 2, "automated": 1, "auto_ratio": 0.5}


class TestDatasetCoverage:
    """Absent file-backed datasets split into fetch gaps and files the repo does not have."""

    @pytest.mark.parametrize(
        ("records", "missing", "not_present"),
        [
            pytest.param(
                {
                    "root_contents": {"type": "root_contents", "data": ["README.md", ".github"]},
                    "github_dir": {"type": "github_dir", "data": ["workflows"]},
                },
                [],
                ["dependabot_config"],
                id="file-not-listed-is-not-present",
            ),
            pytest.param(
                {
                    "root_contents": {"type": "root_contents", "data": ["README.md", ".github"]},
                    "github_dir": {"type": "github_dir", "data": ["dependabot.yml"]},
                },
                ["dependabot_config"],
                [],
                id="file-listed-but-unfetched-is-missing",
            ),
            pytest.param(
                {"root_contents": {"type": "root_contents", "data": ["README.md", ".github"]}},
                ["dependabot_config"],
                [],
                id="github-listing-unknown-stays-missing",
            ),
            pytest.param({}, ["dependabot_config"], [], id="no-listings-stays-missing"),
        ],
    )
    def test_dependabot_config_absence(self, records: dict, missing: list[str], not_present: list[str]) -> None:
        """A dependabot.yml the listings prove absent is complete data; an unproven absence stays a fetch gap.

        Group 2 swallows a 404, so a repository without the file has no record. Scorers read ``missing`` as "not
        fetched" and lower confidence, so only a listing-proven absence may move to ``not_present``.
        """
        # Act
        datasets = vx.extract(records, vx.AxisGroup.B, analysis_now=_NOW)["datasets"]

        # Assert
        assert [name for name in datasets["missing"] if name == "dependabot_config"] == missing
        assert [name for name in datasets["not_present"] if name == "dependabot_config"] == not_present

    def test_branch_protection_absent_without_admin_is_never_missing(self) -> None:
        """List an absent admin-only ``branch_protection`` as optional, never as a fetch gap a scorer degrades.

        Every token without admin rights lacks this record; reporting it ``missing`` invited a -0.1 the rubric assigns
        only to the public protection flag.
        """
        # Arrange
        records = {"default_branch_status": {"type": "default_branch_status", "data": {"protected": True}}}

        # Act
        datasets = vx.extract(records, vx.AxisGroup.B, analysis_now=_NOW)["datasets"]

        # Assert
        assert "branch_protection" not in datasets["missing"]
        assert datasets["optional_absent"] == ["branch_protection"]

    @pytest.mark.parametrize(
        ("records", "missing", "not_present"),
        [
            pytest.param(
                {"docs_dir": {"type": "docs_dir", "data": ["index.md"]}},
                [],
                ["contributing_text", "security_text"],
                id="docs-listing-without-files-is-absent",
            ),
            pytest.param({}, ["contributing_text", "docs_dir", "security_text"], [], id="docs-unlisted-stays-missing"),
            pytest.param(
                {"docs_dir": {"type": "docs_dir", "data": ["CONTRIBUTING.rst"]}},
                ["contributing_text"],
                ["security_text"],
                id="docs-listed-file-unfetched-is-missing",
            ),
        ],
    )
    def test_docs_policy_file_absence(self, records: dict, missing: list[str], not_present: list[str]) -> None:
        """Prove a CONTRIBUTING/SECURITY absence only from a known ``docs/`` listing when the root lists ``docs/``.

        A failed ``docs/<STEM>.md`` fetch used to read as ``not_present`` (decided, no degrader) instead of a gap.
        """
        # Arrange
        listings = {
            "root_contents": {"type": "root_contents", "data": ["README.md", "docs", ".github"]},
            "github_dir": {"type": "github_dir", "data": ["workflows"]},
        }
        wanted = {"contributing_text", "security_text", "docs_dir"}

        # Act
        datasets = vx.extract(listings | records, vx.AxisGroup.B, analysis_now=_NOW)["datasets"]

        # Assert
        assert sorted(wanted.intersection(datasets["missing"])) == missing
        assert sorted(wanted.intersection(datasets["not_present"])) == not_present

    @pytest.mark.parametrize(
        ("root", "missing", "not_present"),
        [
            pytest.param(["README.md", "changes"], ["changelog_headings"], [], id="bare-name-is-inconclusive"),
            pytest.param(["README.md"], [], ["changelog_headings"], id="no-changelog-entry-is-absent"),
        ],
    )
    def test_changelog_absence_agrees_with_the_checkpoint(
        self, root: list[str], missing: list[str], not_present: list[str]
    ) -> None:
        """Never report a changelog proven absent while checkpoint 4 calls it unread; a bare name proves nothing."""
        # Arrange
        records = {"root_contents": {"type": "root_contents", "data": root}}

        # Act
        datasets = vx.extract(records, vx.AxisGroup.A, analysis_now=_NOW)["datasets"]

        # Assert
        assert [name for name in datasets["missing"] if name == "changelog_headings"] == missing
        assert [name for name in datasets["not_present"] if name == "changelog_headings"] == not_present


class TestCli:
    """Command-line contract."""

    def test_prints_compact_group_json_from_long_lines(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Parse records far longer than a file viewer's line limit and print one compact JSON object."""
        # Arrange
        commits = [_iso(1)] * 5000
        data_file = tmp_path / "raw.jsonl"
        data_file.write_text(
            json.dumps({"type": "commits", "timestamp": _NOW, "data": commits}) + "\n", encoding="utf-8"
        )

        # Act
        rc = vx.main(["--data-file", str(data_file), "--group", "A"])

        # Assert
        out = capsys.readouterr().out
        assert rc == 0
        assert out.count("\n") == 1
        assert json.loads(out)["axes"]["2"]["commits_30d"] == 5000

    def test_unreadable_data_file_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Exit 1 with an actionable message when DATA_FILE does not exist."""
        # Act
        rc = vx.main(["--data-file", str(tmp_path / "absent.jsonl"), "--group", "B"])

        # Assert
        assert rc == 1
        assert "cannot read DATA_FILE" in capsys.readouterr().err

    def test_unknown_group_is_rejected(self, tmp_path: Path) -> None:
        """Reject a group outside A, B and C at argument parsing."""
        # Act
        with pytest.raises(SystemExit) as exc:
            vx.main(["--data-file", str(tmp_path / "raw.jsonl"), "--group", "D"])

        # Assert
        assert exc.value.code == 2

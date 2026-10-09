#!/usr/bin/env python
"""vitality_extract.py — extract the per-axis metrics one oss:repo-warden group scores from.

The vitality DATA_FILE is JSONL with one dataset per line; single lines reach hundreds of
kilobytes (open PRs with check rollups, contributor stats with weekly buckets). Reading it
with a line-truncating file viewer silently drops whole datasets, after which a scorer
reports "no commits / no CI / no README" for data that exists. This script parses the file
with ``json`` and prints one compact JSON object holding exactly the counts, dates,
presence flags and derived ratios that the axis rubrics in
``skills/_shared/vitality-scoring-group-{a,b,c}.md`` consume.

It also applies the rubric's scoring rules, so two scorer models reading the same data
report the same numbers by construction rather than by agreeing on prose: every axis
carries ``band``, ``score``, ``conf`` and ``conf_degraders`` (band-only axes also
``clauses_held`` and ``red_held``). The rubric files stay the documented definition this
code implements; the agent copies these values and only writes the signal and notes.

Groups: ``A`` = Axes 1, 2, 5, 6 · ``B`` = Axes 4, 7, 8 · ``C`` = Axes 3, 9 (plus the
Axis 2 band, which one Axis 3 🔴 clause reads).

Conventions baked into the extraction (each mirrors a rubric rule):

- Time windows are measured from ANALYSIS_NOW: ``--analysis-now`` when given, else the
  first record's ``timestamp``, else the current time.
- Bot accounts: GitHub's own ``is_bot`` flag when the dataset carries it; otherwise a
  login ending in ``[bot]`` or ``-bot``, starting with ``app/`` (how ``gh`` renders GitHub
  App authors), or one of the known automation names. Unknown names count as human —
  under-filtering is the conservative choice the rubric asks for.
- Regex checkpoints use the rubric's patterns case-insensitively; ``.`` never crosses a
  newline, matching line-based ``grep``.
- A metric whose input is missing is ``null`` with an ``available: false`` or a note;
  never ``0``, which would read as a measured value.
- Thresholds compare unrounded metrics; only the printed metrics are rounded (``_DISPLAY_DIGITS``).
  Rounding first moved boundary values across a band line: a close rate of 159/200 printed as
  0.8 and held the ``≥0.8`` clause.

Usage:
    vitality_extract.py --data-file <path> --group A|B|C [--analysis-now <epoch-seconds>]

Exit: 0 on success; 1 when DATA_FILE cannot be read; 2 on invalid arguments (argparse).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import pairwise
from typing import Any

from assemble_vitality_data import (
    DEPENDABOT_CONFIG_NAMES,
    GROUP2_AMBIGUOUS,
    GROUP2_EVIDENCE,
    SEARCH_RESULT_CAP,
    is_changelog_entry,
    is_changelog_file,
    listings_of,
)

_DAY = 86400
#: Logins of automation accounts that carry no ``[bot]``/``-bot`` suffix.
_KNOWN_BOTS = frozenset(
    {
        "pre-commit-ci",
        "mergify",
        "allcontributors",
        "renovate",
        "dependabot",
        # GraphQL drops the ``[bot]`` suffix; these answered PRs within seconds in real data fetched before
        # ``__typename`` was requested, so older DATA_FILEs still need them by name.
        "codecov",
        "copilot-pull-request-reviewer",
        "socket-security",
        "claassistant",
    }
)

#: Optional quote before a tox env, label or nox session name; kept out of the raw strings so the compiled pattern
#: (quoted verbatim in the Group A rubric) carries no backslash before the quote.
_OPTIONAL_QUOTE = "[\"']?"
#: A tox factor naming a non-test job; an env carrying one anywhere (``py311-lint``, ``lint-py311``, ``py39-mypy``,
#: ``py312-docs``, ``py311-linters``, ``py312-typecheck``, ``py3-pre-commit``, ``py311-coverage-report``) is not a test
#: env even beside a Python factor. ``coverage`` alone stays a test factor: ``py311-coverage`` usually runs the tests
#: under coverage, and an env of that name that only combines reports is an accepted gap.
_NON_TEST_FACTOR = (
    r"(?:lint(?:ers?|ing)?|docs|typing|typecheck|mypy|type|fmt|format|style|build|publish|release|upload|fuzz"
    r"|pre-commit|precommit|report)"
)
#: A tox env that runs tests: one carrying a versioned Python factor (``py311``, ``ci-py311``, ``ci-pypy3``,
#: ``py311-django42``, or ``ci-py$(…)`` with the version templated in), the whole name ``py``, ``pypy`` or ``ci``, or a
#: ``test``/``tests``/``testing`` env — and no ``_NON_TEST_FACTOR`` among its ``-``-separated factors. A factor is never
#: a prefix match: ``py-lint``, ``ci-lint`` and ``testpypi-upload`` are not test envs, ``py311-types-x`` still is.
_TOX_TEST_ENV = (
    rf"(?!(?:[\w.]+-)*{_NON_TEST_FACTOR}(?!\w))"
    r"(?:(?:[\w.]+-)*py(?:py)?(?:\d[\w.]*|\$)|(?:py|pypy|ci)(?![\w-])|test(?:s|ing)?(?!\w))"
)
#: A test-shaped session, label, script or make target name: ``test``, ``tests``, ``testing``, then a non-word
#: character (``tests-3.11``, ``test:cov``), never a longer word such as ``testpypi``.
_TEST_NAME = r"test(?:s|ing)?(?!\w)"
#: Command-line flags before the part that names the job, each with or without a value: ``-p``, ``-vv``,
#: ``-c tox.ini``, ``-p 3.11``, ``--filter pkg``. A value never starts with ``-`` and never crosses a line.
_RUNNER_FLAGS = r"(?:\s+-\S+(?:[ \t]+[^-\s]\S*)?)*?"
#: Axis 5 checkpoint 2 — workflow runs tests. Runners driven through tox, nox, hatch, make or a JS package manager
#: count only in a test shape: a tox ``-e`` env list holding a test env (see ``_TOX_TEST_ENV``; tox flags with or
#: without a value may sit before ``-e``, after the ``run``/``r``/``run-parallel``/``p`` subcommand), a tox 4 ``-f``
#: factor filter whose first factor is test-shaped (``tox -f py311``), a tox ``test`` label (``-m test``), a
#: ``toxenv``/``TOXENV`` assignment naming a test env (the matrix entry behind ``tox -e ${{ matrix.toxenv }}``), a
#: ``test*`` nox session or tag (``-s tests``, ``-t tests``, after any nox flags), ``hatch test`` or
#: ``hatch run [env:]test``, ``npm``/``yarn``/``pnpm [flags] [run] test`` (``pnpm -r test``), ``npm t``,
#: ``python -m unittest`` and ``make test``. Bare ``tox`` and other env names (``lint``, ``fuzz``, ``pylint``,
#: ``docs``, ``py-lint``, ``ci-lint``, ``py311-lint``, ``testpypi-upload``) never count; neither does
#: ``tox -e ${{ matrix.toxenv }}`` alone, whose env the data does not show.
_TESTS_RE = re.compile(
    r"pytest|jest|cargo test|go test|mvn test|rspec|phpunit"
    rf"|\b(?:npm|yarn|pnpm){_RUNNER_FLAGS}\s+(?:run\s+)?{_TEST_NAME}|\bnpm{_RUNNER_FLAGS}\s+t(?![\w.:-])"
    rf"|\btox(?:\s+(?:run-parallel|run|r|p))?{_RUNNER_FLAGS}\s+"
    rf"(?:(?:-e|--env)[\s=]*{_OPTIONAL_QUOTE}(?:[\w.-]+,)*{_TOX_TEST_ENV}|(?:-f|--factors)[\s=]*{_OPTIONAL_QUOTE}"
    rf"{_TOX_TEST_ENV}|(?:-m|--labels)[\s=]*{_OPTIONAL_QUOTE}{_TEST_NAME})"
    rf"|\btox[-_]?env{_OPTIONAL_QUOTE}[ \t]*[:=][ \t]*\[?[ \t]*(?:{_OPTIONAL_QUOTE}[\w.-]+{_OPTIONAL_QUOTE}[ \t]*,[ \t]*)*"
    rf"{_OPTIONAL_QUOTE}{_TOX_TEST_ENV}"
    rf"|\bnox{_RUNNER_FLAGS}\s+(?:-s|--sessions?|-t|--tags)[\s=]*{_OPTIONAL_QUOTE}{_TEST_NAME}"
    rf"|\bhatch\s+(?:test\b|run\s+(?:[\w.-]+:)?{_TEST_NAME})|python[\d.]*\s+-m\s+unittest|\bmake\s+{_TEST_NAME}",
    re.IGNORECASE,
)
#: Axis 5 checkpoint 3 — workflow runs a linter or formatter.
_LINT_RE = re.compile(r"ruff|flake8|eslint|prettier|rubocop|golangci|black|mypy", re.IGNORECASE)
#: Axis 5 checkpoint 4 — SAST or security scan, Actions-workflow, dependency, secret and supply-chain scanners included
#: (``dependency-review-action`` blocks vulnerable dependency changes, ``scorecard-action`` runs OpenSSF Scorecard).
_SAST_RE = re.compile(
    r"codeql|semgrep|sonar|snyk|trivy|bandit|zizmor|osv-scanner|gitleaks|trufflehog|pip-audit"
    r"|dependency-review-action|scorecard-action",
    re.IGNORECASE,
)
#: Axis 6 checkpoint 2 — README install section.
_INSTALL_RE = re.compile(r"install|pip install|npm install|cargo add|brew install", re.IGNORECASE)
#: Axis 6 checkpoint 3 — README usage section.
_USAGE_RE = re.compile(r"usage|quickstart|getting started|example", re.IGNORECASE)
#: Axis 6 checkpoint 7 — CONTRIBUTING dev setup.
_DEV_SETUP_RE = re.compile(r"setup|local.*install|dev.*env|getting started", re.IGNORECASE)
#: Axis 6 checkpoint 8 — CONTRIBUTING PR/review process.
_PR_PROCESS_RE = re.compile(r"pull.request|review.*process|merge.*process|workflow", re.IGNORECASE)
#: Axis 6 checkpoint 9 — CONTRIBUTING code style or lint guidance.
_STYLE_RE = re.compile(r"code.*style|lint|format|coding.*standard|ruff|mypy|eslint|prettier", re.IGNORECASE)
#: Axis 2 abandonment — subjects that name the repository itself. "this package/module/tool/plugin ..." stays out: a
#: README uses those words for sub-components ("foo.bar: this module is deprecated, use foo.baz").
_ABANDON_SUBJECT = r"(?:project|repo|repository|library|fork)"
#: Axis 2 abandonment status a subject is declared to have: "is (now) deprecated", "has been archived", "is no longer
#: (actively) maintained", "is not actively maintained", "is not maintained anymore", ...
_ABANDON_STATUS = (
    r"(?:is|has\s+been)\s+(?:now\s+)?"
    r"(?:deprecated|unmaintained|archived|abandoned|discontinued|no\s+longer\s+(?:actively\s+|being\s+)?maintained"
    r"|not\s+(?:being\s+)?(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))\b"
)
#: Status a banner line declares: a status word, or "no longer / not actively maintained" with the repository as the
#: implied subject.
_ABANDON_WORD = (
    r"(?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained"
    r"|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))"
)
#: Warning sign marking a banner: ``⚠️``, ``⛔``, ``❗``, ``❌``, ``🚨``, ``🚫``, ``🛑`` (decorative emoji such as
#: ``📦`` are not warnings).
_WARNING_SIGN = r"[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?"
#: Opening and closing marks around a description's status: bold/italic, a bracket, a warning sign.
_ABANDON_OPEN = rf"(?:\*{{1,2}}|_{{1,2}}|\[|\(|{_WARNING_SIGN}[ \t]*)"
_ABANDON_CLOSE = rf"(?:\*{{1,2}}|_{{1,2}}|\]|\)|[ \t]*{_WARNING_SIGN})"
#: Description tail — the status ends the description or its sentence ("**DEPRECATED**", "Archived.", "No longer
#: maintained. This ..."), or a redirect follows it ("DEPRECATED use bar", "DEPRECATED, please see bar"). A ``:`` that
#: introduces anything else names another subject ("Deprecated: Python 3.8 support was dropped"), and a following noun
#: makes the status an adjective ("Deprecated APIs shim").
_ABANDON_TAIL = (
    rf"{_ABANDON_CLOSE}*[ \t]*(?:[.!]?{_ABANDON_CLOSE}*[ \t]*$|[.!]{_ABANDON_CLOSE}*(?=[ \t])"
    rf"|[.:!,;—–-]*{_ABANDON_CLOSE}*[ \t]*(?:please[ \t]+)?(?:use|see|moved|replaced|superseded)\b)"
)
#: Line start of a statement: indentation and blockquote markers.
_ABANDON_LINE = r"^[ \t]*(?:>[ \t]*)*"
#: Axis 2 abandonment override, sentence forms — a statement in the repository description or the README's first 500
#: bytes that the repository itself is discontinued: ``this <project|repo|repository|library|fork> <status>`` or "no
#: longer maintaining this <project|...>". A bare keyword never counts: "drop-in replacement for the deprecated foo",
#: "Python 2 support is deprecated", "The 1.x branch is no longer maintained" or "Deprecated: Python 3.8 support was
#: dropped" describe something else, and zeroed the axis of actively maintained repositories. README banners are read
#: line by line by content (``_BANNER_RE``, ``_TITLE_BANNER_RE``), the description by ``_ABANDON_DESCRIPTION_RE``.
_ABANDON_RE = re.compile(
    rf"\bthis\s+{_ABANDON_SUBJECT}\s+{_ABANDON_STATUS}"
    rf"|\bno\s+longer\s+(?:actively\s+)?maintain(?:ing)?\s+this\s+{_ABANDON_SUBJECT}\b",
    re.IGNORECASE,
)
#: Replacement a banner redirect names: one link, URL, code span or word — never a flag (``--new``), and never "below",
#: "above", "here" or "of", which point into the README or turn "use" into a noun ("Deprecated use of ``foo()``"). The
#: backtick is spelled ``\x60`` so the pattern quotes verbatim inside a Markdown code span in the Group A rubric.
_BANNER_TARGET = (
    r"(?:\[[^\]]*\]\([^)\s]*\)|\x60[^\x60]+\x60|<?https?://\S+?>?|(?!(?:below|above|here|of)\b)[\w@][\w./@-]*)"
)
#: Axis 2 abandonment banner — the content of one README line once its markup is stripped (indentation, blockquote and
#: heading marks, HTML tags, a GitHub alert label, bold/italic marks, warning signs; see :func:`_banner_line`): the
#: status alone, optionally in a tag bracket ("DEPRECATED", "[Archived]", "No longer maintained."), or the status and a
#: redirect to one replacement that ends the line, optionally with "instead" ("DEPRECATED use bar", "Deprecated: use
#: bar instead.", "No longer maintained. See https://…"). The content decides, whatever the markup: "# DEPRECATED",
#: "<h1>DEPRECATED</h1>", "**DEPRECATED**" and "> [!WARNING] Deprecated" are one banner. A redirect carrying more
#: names the deprecated feature ("use --new-flag instead of --old-flag", "see below for removed flags", "see CHANGELOG
#: for the migration of old APIs"), and a status followed by any other sentence names another subject ("Archived.
#: Releases before 2.0 live in the old repo.").
_BANNER_RE = re.compile(
    rf"[\[(]?{_ABANDON_WORD}[\])]?[ \t]*(?:[.!]*|(?P<redirect>[.:!,;—–-]*[ \t]*(?:please[ \t]+)?"
    rf"(?:use|see|(?:moved|replaced|superseded)(?:[ \t]+(?:to|by))?)[ \t]+{_BANNER_TARGET}(?:[ \t]+instead)?[.!]*))",
    re.IGNORECASE,
)
#: Axis 2 abandonment banner on the README title (a ``#`` heading, ``<h1>`` or ``===``-underlined line), its markup
#: stripped like ``_BANNER_RE``'s: the status beside the title text — in parentheses or brackets after it, after a
#: dash or colon, or in a leading tag bracket ("foo (DEPRECATED)", "foo — DEPRECATED", "foo [ARCHIVED]", "[DEPRECATED]
#: foo"). A status qualifying a noun in the title ("Deprecated APIs") is no banner.
_TITLE_BANNER_RE = re.compile(
    rf"\S.*?(?:[ \t]+\({_ABANDON_WORD}\)|[ \t]+\[{_ABANDON_WORD}\]|[ \t]*[—–:][ \t]*{_ABANDON_WORD}"
    rf"|[ \t]+-[ \t]+{_ABANDON_WORD})[.!]*|\[{_ABANDON_WORD}\][ \t]+\S.*",
    re.IGNORECASE,
)
#: README bytes the Axis 2 abandonment override reads — where a status banner sits.
_ABANDON_HEAD_BYTES = 500
#: A list item or table row (Markdown or HTML) names one entry ("- Deprecated", "| Deprecated |"), never the repository.
_ITEM_LINE_RE = re.compile(r"(?:[-*+]|\d+[.)])[ \t]|\||<(?:li|td|th)\b", re.IGNORECASE)
#: ATX heading: its level marks and its text without the optional closing ``#`` run.
_ATX_HEADING_RE = re.compile(r"(?P<marks>#{1,6})[ \t]+(?P<text>.*?)(?:[ \t]+#+)?[ \t]*")
#: HTML heading tag carrying its level.
_HTML_HEADING_RE = re.compile(r"<h(?P<level>[1-6])\b", re.IGNORECASE)
#: Setext underline making the line above it a level-1 heading.
_SETEXT_TITLE_RE = re.compile(r"[ \t]*=+[ \t]*")
#: Markup stripped before a banner line's content is read: line-start indentation and blockquote marks, HTML tags (an
#: autolink such as ``<https://…>`` stays), a GitHub alert label, bold/italic marks and warning signs. Strikethrough
#: (``~~``) stays: a struck-through status is withdrawn.
_LINE_START_RE = re.compile(_ABANDON_LINE)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^>]*)?/?>")
_ALERT_LABEL_RE = re.compile(r"\[!(?:note|tip|important|warning|caution)\]", re.IGNORECASE)
_EMPHASIS_RE = re.compile(r"\*+|(?<!\w)_+|_+(?!\w)")
_WARNING_SIGN_RE = re.compile(_WARNING_SIGN)
#: Axis 2 abandonment override, description only: the description is about the repository by construction, so a
#: leading status also counts when a tag bracket closes it ("[UNMAINTAINED] foo"), punctuation follows it
#: ("Archived: old foo", "DEPRECATED - use bar"), "in favor of" follows it, or it qualifies a repository subject that
#: "of" or the clause's end follows ("Unmaintained fork of foo", "Unmaintained library."). The description tail comes
#: first, so a description both patterns match yields one statement. The same lines in a README may introduce a list of
#: deprecated features; "Deprecated APIs shim", "Deprecated library finder" and "Deprecated project template" name
#: what a tool handles and stay quiet.
_ABANDON_DESCRIPTION_RE = re.compile(
    rf"^[ \t]*{_ABANDON_OPEN}*{_ABANDON_WORD}(?:{_ABANDON_TAIL}|[\])]|{_ABANDON_CLOSE}*[ \t]*[:,;—–-]"
    rf"|{_ABANDON_CLOSE}*[ \t]+(?:in[ \t]+favou?r[ \t]+of\b|{_ABANDON_SUBJECT}(?:[ \t]+of\b|[ \t]*(?:[.!,;:—–-]|$))))",
    re.IGNORECASE,
)
#: Axis 8 secondary signal — dependency-update commit message.
_DEP_COMMIT_RE = re.compile(
    r"^(bump|chore\(deps\)|build\(deps\)|deps:|dependabot|update deps|upgrade deps)", re.IGNORECASE
)
#: Axis 9D — automated dependency-bump commit message.
_AUTO_COMMIT_RE = re.compile(
    r"^(bump|chore\(deps\)|build\(deps\)|dependabot|renovate|update deps|upgrade deps)", re.IGNORECASE
)
#: Advisory pre-release tag (``0.x`` or alpha/beta/rc).
_PRE_RELEASE_RE = re.compile(r"^v?0\.|alpha|beta|rc", re.IGNORECASE)

_RENOVATE_FILES = frozenset({"renovate.json", "renovate.json5", ".renovaterc", ".renovaterc.json", ".renovaterc.json5"})
#: Axis 7 checkpoint 3 — code-of-conduct file stems (GitHub accepts the hyphenated spelling too).
_CODE_OF_CONDUCT_STEMS = frozenset({"code_of_conduct", "code-of-conduct"})
#: Header ``fetch_gh_data_group2.py`` writes before each workflow file; stripped so a file *name* never meets a
#: content checkpoint (``codeql.yml`` running only ``make check`` is not a security scan).
_WORKFLOW_HEADER_RE = re.compile(r"^--- workflow: .* ---$", re.MULTILINE)
#: Comments/reviews fetched per issue or PR; an item with this many events and no human response may have one later.
_EVENT_CAP = 10
#: Axis 6 checkpoint thresholds; a value on the threshold meets the checkpoint (the better outcome), like every 🟢
#: clause: a README of at least this many bytes (checkpoint 1) and a newest changelog entry at most this old (4).
_README_MIN_BYTES = 500
_CHANGELOG_MAX_AGE_DAYS = 365
#: Axis 2 maintenance backport: a release at most this many days old (with ≥3 commits in 90 days) lifts a >60-day commit
#: gap from 🔴 to 🟡; exactly 180 days still lifts it.
_BACKPORT_RELEASE_DAYS = 180
#: Run conclusions left out of the Axis 5 checkpoint 5 sample: the run executed no job (``skipped`` — a conditional
#: workflow whose trigger did not apply — or ``neutral``), was superseded (``cancelled``) or has not concluded
#: (``pending``). Every other conclusion counts, ``failure``, ``action_required``, ``timed_out`` and unknown values
#: included, so an unfamiliar outcome is never silently dropped.
_RUN_EXCLUDED = frozenset({"pending", "skipped", "neutral", "cancelled"})
#: Counted runs in the Axis 5 checkpoint 5 sample, newest first.
_RUN_SAMPLE = 20
#: Run events that exercise the default branch's own CI: a push, a schedule, a manual dispatch and the merge queue.
#: Every other event stays out of the sample. The runs are fetched for the default branch (``branch=``), which matches
#: a run's head branch, so a pull request opened from a fork's own ``main`` still comes back; ``workflow_run`` jobs run
#: in the default branch's context while reacting to pull requests (PR-comment bots), and ``dynamic`` runs are GitHub's
#: own (Dependabot updates, Copilot review). None of them measures the project's CI health.
_CI_EVENTS = ("push", "schedule", "workflow_dispatch", "merge_group")
#: Display precision per metric field. Thresholds compare the unrounded value (module docstring); a float field not
#: listed here prints unrounded rather than wrong.
_DISPLAY_DIGITS: dict[str, int] = {
    **dict.fromkeys(
        (
            "pct_responded_7d",
            "pct_unresponded",
            "stale_pct",
            "abandoned_pct",
            "coverage_pct",
            "ci_pass_rate_pct",
            "top_contributor_pct",
            "retention_pct",
            "days_since_last_commit",
            "days_since_last_release",
            "release_cadence_days",
            "p90_age_days",
            "window_days_covered",
        ),
        1,
    ),
    **dict.fromkeys(
        (
            "median_issue_response_days",
            "median_pr_response_days",
            "median_open_age_days",
            "median_30d_days",
            "median_90d_days",
            "close_rate",
            "merge_rate",
            "closed_without_merge_ratio",
            "trend_ratio",
            "active_ratio",
        ),
        2,
    ),
    **dict.fromkeys(("shrinkage_ratio", "auto_ratio"), 3),
}
#: ``commits_50`` author placeholder when GitHub resolves neither a login nor a git name.
_UNKNOWN_AUTHOR = "unknown"
#: Axis 8 SECURITY.md depth — a contact e-mail address.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
#: Axis 8 SECURITY.md depth — a response SLA (a number next to hour/day/week).
_SLA_RE = re.compile(r"\d+\s*(?:business\s+|working\s+)?(?:hours?|days?|weeks?)\b", re.IGNORECASE)
#: Month names and abbreviations, for changelog dates.
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_MONTH_NUMBERS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
    )
}
#: Changelog date formats the rubric names: ``YYYY-MM-DD``, ``DD Month YYYY``, ``Month YYYY`` (plus ``Month DD, YYYY``).
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
#: Day number with an optional two-letter ordinal suffix (``5th``, ``22``).
_DAY_NUMBER = r"(\d{1,2})[a-z]{0,2}"
_DAY_MONTH_YEAR_RE = re.compile(rf"\b{_DAY_NUMBER}\s+{_MONTH},?\s+(\d{{4}})\b", re.IGNORECASE)
_MONTH_DAY_YEAR_RE = re.compile(rf"\b{_MONTH}\s+{_DAY_NUMBER},?\s+(\d{{4}})\b", re.IGNORECASE)
_MONTH_YEAR_RE = re.compile(rf"\b{_MONTH}\s+(\d{{4}})\b", re.IGNORECASE)
#: A version number in a changelog heading or release tag (``26.10.0``, ``v1.2.3``, ``21.12b0``, ``1.0.0-rc.1``); at
#: least one dot, so an ISO date (``2024-01-05``) never reads as a version.
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+)+[\w.+-]*)", re.IGNORECASE)


class Checkpoint(str, Enum):
    """State of one rubric checkpoint.

    ``indeterminate`` means the input that decides the checkpoint was not fetched or could not be read; the rubric
    scores it as unmet (strict lower bound) and reports the upper bound separately.
    """

    MET = "met"
    UNMET = "unmet"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not_applicable"


class AxisGroup(str, Enum):
    """Axis group a oss:repo-warden instance scores."""

    A = "A"
    B = "B"
    C = "C"


#: Datasets each group reads; reported back as used / missing / partial.
GROUP_DATASETS: dict[AxisGroup, tuple[str, ...]] = {
    AxisGroup.A: (
        "responsiveness_gql",
        "commits",
        "releases",
        "readme_content",
        "repo_metadata",
        "ci_workflows",
        "ci_runs",
        "workflows_list",
        "workflow_files",
        "root_contents",
        "github_dir",
        "docs_dir",
        "changelog_headings",
        "contributing_text",
    ),
    AxisGroup.B: (
        "open_issues",
        "closed_issues",
        "open_prs",
        "closed_prs",
        "review_coverage_gql",
        "root_contents",
        "github_dir",
        "docs_dir",
        "codeowners_text",
        "contributing_text",
        "security_text",
        "default_branch_status",
        "branch_protection",
        "contributor_stats",
        "dependabot_alerts",
        "secret_scanning_alerts",
        "dependabot_config",
        "ci_workflows",
        "commits_50",
    ),
    # Axis 2 inputs too: one Axis 3 🔴 clause needs the Axis 2 band.
    AxisGroup.C: (
        "contributor_stats",
        "commits_50",
        "merged_prs_90d",
        "open_issues",
        "commits",
        "releases",
        "readme_content",
        "repo_metadata",
    ),
}

#: Datasets only some tokens can fetch (``branch_protection`` needs admin rights); absence is expected, never a
#: fetch gap. Axis 7 checkpoint 6 reads the public ``default_branch_status`` instead.
_OPTIONAL_DATASETS = frozenset({"branch_protection"})


@dataclass(frozen=True)
class DataView:
    """Read access to DATA_FILE records, last record per type.

    Attributes:
        records: Record per ``type``.
        now: ANALYSIS_NOW in epoch seconds.
    """

    records: dict[str, dict[str, Any]]
    now: float

    def has(self, name: str) -> bool:
        """Tell whether a record of type ``name`` exists."""
        return name in self.records

    def data(self, name: str) -> Any:
        """Return the ``data`` payload of ``name``, or ``None`` when the record is absent."""
        record = self.records.get(name)
        return None if record is None else record.get("data")

    def items(self, name: str) -> list[Any]:
        """Return the payload as a list; anything else (absent, ``"403"``, object) yields ``[]``."""
        data = self.data(name)
        return data if isinstance(data, list) else []

    def text(self, name: str) -> str | None:
        """Return the payload when it is a string, else ``None``."""
        data = self.data(name)
        return data if isinstance(data, str) else None

    def partial(self, name: str) -> bool:
        """Tell whether the record is flagged truncated."""
        return bool(self.records.get(name, {}).get("partial"))

    def names(self, name: str) -> frozenset[str] | None:
        """Lower-case a listing record's file names; ``None`` when the listing is absent."""
        data = self.data(name)
        return frozenset(str(item).lower() for item in data) if isinstance(data, list) else None

    def age_days(self, value: Any) -> float | None:
        """Days between an ISO timestamp and ANALYSIS_NOW."""
        stamp = parse_ts(value)
        return None if stamp is None else (self.now - stamp) / _DAY

    def within(self, value: Any, days: int) -> bool:
        """Tell whether an ISO timestamp falls inside the last ``days`` days."""
        age = self.age_days(value)
        return age is not None and age <= days


# --- primitives -------------------------------------------------------------


def parse_ts(value: Any) -> float | None:
    """Parse a GitHub ISO-8601 timestamp to epoch seconds.

    Args:
        value: Timestamp string such as ``2026-10-05T23:42:56Z``; naive values are UTC.

    Returns:
        Epoch seconds, or ``None`` for non-strings and unparsable text.

    Examples:
        >>> parse_ts("1970-01-02T00:00:00Z")
        86400.0
        >>> parse_ts(None) is None
        True
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def is_bot(login: Any, flag: Any = None) -> bool:
    """Tell whether an account is automation.

    Args:
        login: Account login; ``None`` (deleted user) counts as human.
        flag: GitHub's ``is_bot`` field when the dataset carries it.

    Returns:
        ``True`` for bots.

    Examples:
        >>> is_bot("dependabot[bot]"), is_bot("app/pre-commit-ci"), is_bot("ci-bot")
        (True, True, True)
        >>> is_bot("alice"), is_bot(None), is_bot("alice", flag=True)
        (False, False, True)
    """
    if flag is True:
        return True
    if not isinstance(login, str) or not login:
        return False
    lowered = login.lower()
    return lowered.endswith(("[bot]", "-bot")) or lowered.startswith("app/") or lowered in _KNOWN_BOTS


def _login(actor: Any) -> str | None:
    """Return ``actor["login"]`` for GraphQL/``gh`` author objects, else ``None``."""
    return actor.get("login") if isinstance(actor, dict) else None


def _is_bot_actor(actor: Any) -> bool:
    """Tell whether a GraphQL/``gh`` author object is automation.

    GraphQL returns an app's login without its ``[bot]`` suffix, so the actor's
    ``__typename`` (``Bot``) or ``is_bot`` flag decides when present; the login
    rules of :func:`is_bot` cover data fetched without either field.

    Examples:
        >>> _is_bot_actor({"login": "codecov-ai", "__typename": "Bot"}), _is_bot_actor({"login": "alice"})
        (True, False)
    """
    if not isinstance(actor, dict):
        return False
    return is_bot(actor.get("login"), actor.get("__typename") == "Bot" or actor.get("is_bot") is True)


def _dig(obj: Any, *keys: str) -> Any:
    """Walk nested dicts, returning ``None`` as soon as a level is missing or not a dict."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _gql_nodes(payload: Any, connection: str) -> list[Any]:
    """Return ``data.repository.<connection>.nodes`` from a GraphQL response, or ``[]``."""
    nodes = _dig(payload, "data", "repository", connection, "nodes")
    return nodes if isinstance(nodes, list) else []


def _pct(numerator: int, denominator: int) -> float | None:
    """Percentage, unrounded (see ``_DISPLAY_DIGITS``); ``None`` when the denominator is zero.

    Examples:
        >>> _pct(1, 4), _pct(1, 0)
        (25.0, None)
    """
    return 100 * numerator / denominator if denominator else None


def _median(values: list[float]) -> float | None:
    """Median, unrounded (see ``_DISPLAY_DIGITS``); ``None`` for an empty sample."""
    return statistics.median(values) if values else None


def _display(value: Any, key: str | None = None) -> Any:
    """Round every float metric to its ``_DISPLAY_DIGITS`` precision for output, recursing into dicts and lists.

    Args:
        value: Metric value, or a dict/list of them.
        key: Field name the value sits under.

    Returns:
        The same structure with listed float fields rounded; every other value unchanged.

    Examples:
        >>> _display({"close_rate": 0.795, "issues": {"stale_pct": 33.333}, "conf": 0.8})
        {'close_rate': 0.8, 'issues': {'stale_pct': 33.3}, 'conf': 0.8}
    """
    if isinstance(value, dict):
        return {name: _display(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_display(item, key) for item in value]
    if isinstance(value, float) and key in _DISPLAY_DIGITS:
        return round(value, _DISPLAY_DIGITS[key])
    return value


def _matches(pattern: re.Pattern[str], texts: Iterable[str]) -> list[str]:
    """Distinct lower-cased matches of ``pattern`` across ``texts``, sorted.

    Whole matches, never group contents: ``findall`` returns the groups of a pattern that has any, which turned a
    grouped alternation into tuples.

    Examples:
        >>> _matches(re.compile(r"tox -e (py|ci)", re.IGNORECASE), ["run: tox -e ci-py311", "TOX -E py"])
        ['tox -e ci', 'tox -e py']
    """
    return sorted({match.group(0).lower() for text in texts for match in pattern.finditer(text)})


def _searches(pattern: re.Pattern[str], text: str | None) -> bool | None:
    """Regex presence in ``text``; ``None`` when the text was not fetched."""
    return None if text is None else bool(pattern.search(text))


def _in_any(name: str, *listings: frozenset[str] | None) -> bool:
    """Tell whether a lower-case file name appears in any available listing."""
    return any(listing is not None and name in listing for listing in listings)


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    """Ratio, unrounded (see ``_DISPLAY_DIGITS``); ``None`` when an input is missing or the denominator is zero.

    Examples:
        >>> _ratio(4, 5), _ratio(159, 200), _ratio(0, 0), _ratio(None, 5)
        (0.8, 0.795, None, None)
    """
    return numerator / denominator if numerator is not None and denominator else None


def _below(value: float | None, threshold: float) -> bool:
    """Tell whether a metric is strictly below ``threshold``; a missing metric never is."""
    return value is not None and value < threshold


def _above(value: float | None, threshold: float) -> bool:
    """Tell whether a metric is strictly above ``threshold``; a missing metric never is."""
    return value is not None and value > threshold


def _at_least(value: float | None, threshold: float) -> bool:
    """Tell whether a metric reaches ``threshold`` (inclusive, so a boundary value belongs to the better band)."""
    return value is not None and value >= threshold


def _at_most(value: float | None, threshold: float) -> bool:
    """Tell whether a metric stays within ``threshold`` (inclusive, so a boundary value belongs to the better band).

    Every 🟢 clause of the "lower is better" kind uses this; every 🔴 condition stays strict (``_above``/``_below``),
    so a value on a boundary two band lines share is never owned by the worse band.

    Examples:
        >>> _at_most(10.0, 10), _at_most(10.1, 10), _at_most(None, 10)
        (True, False, False)
    """
    return value is not None and value <= threshold


# --- scoring rules --------------------------------------------------------------


class Band(str, Enum):
    """Axis status label, in the rubric's glyphs."""

    RED = "🔴"
    YELLOW = "🟡"
    GREEN = "🟢"
    UNAVAILABLE = "⚪"


#: In-band placement for band-only axes: anchor and maximum score per band. 🟢 is fixed at 10: it requires every 🟢
#: clause, so counting held clauses on top of an anchor only made the maximum depend on the axis's clause count
#: (two-clause axes topped out at 9).
_BAND_RANGE: dict[Band, tuple[int, int]] = {Band.RED: (1, 3), Band.YELLOW: (4, 6), Band.GREEN: (10, 10)}
#: Confidence removed per indeterminate checkpoint that no applied listed degrader covers.
_INDETERMINATE_DELTA = 0.05
#: Confidence floor per axis — the Weights table in ``skills/_shared/vitality-scoring.md``.
_CONF_FLOOR = {"1": 0.3, "2": 0.2, "3": 0.4, "4": 0.3, "5": 0.4, "6": 0.5, "7": 0.6, "8": 0.2, "9": 0.3}
#: Fixed confidence of the Axis 3 commit-author fallback; replaces the degrader formula.
_CONF_AXIS3_FALLBACK = 0.5
#: Fixed confidence of Axis 8 partial scoring (Dependabot alerts unavailable); replaces the degrader formula.
_CONF_AXIS8_PARTIAL = 0.4
#: Axis 8 partial points at which the label turns 🟡 (below: 🔴).
_PARTIAL_YELLOW_POINTS = 4


@dataclass(frozen=True)
class Degrader:
    """One listed confidence degrader whose condition the data shows.

    Attributes:
        cause: The Weights-table condition, worded in extractor fields.
        delta: Confidence it removes.
        covers: Checkpoints whose indeterminate state this cause explains; they cost no extra -0.05.
    """

    cause: str
    delta: float
    covers: frozenset[str] = frozenset()


def _degraders(*candidates: tuple[bool, Degrader]) -> list[Degrader]:
    """Keep the degraders whose condition holds, in listed order."""
    return [degrader for holds, degrader in candidates if holds]


def confidence(floor: float, degraders: list[Degrader], indeterminate: Iterable[str] = ()) -> dict[str, Any]:
    """Compute an axis confidence from the listed degraders whose condition the data shows.

    ``conf`` = 1.0 − each applied degrader − 0.05 per indeterminate checkpoint that no applied degrader covers,
    never below ``floor``. A listed degrader replaces the per-checkpoint -0.05 for the checkpoints its cause explains;
    the two never add up for one checkpoint.

    Args:
        floor: The axis confidence floor.
        degraders: Applied listed degraders.
        indeterminate: Checkpoint names in the ``indeterminate`` state.

    Returns:
        ``{"conf", "conf_degraders": [{"cause", "delta"}]}`` listing every deduction made.

    Examples:
        >>> result = confidence(0.4, [Degrader("workflow_files partial", 0.1, frozenset({"3"}))], ["3", "5"])
        >>> result["conf"], [d["cause"] for d in result["conf_degraders"]]
        (0.85, ['workflow_files partial', 'checkpoint 5 indeterminate'])
        >>> confidence(0.6, [Degrader("a", 0.3), Degrader("b", 0.3)])["conf"]
        0.6
    """
    covered = frozenset().union(*(degrader.covers for degrader in degraders))
    applied = degraders + [
        Degrader(f"checkpoint {name} indeterminate", _INDETERMINATE_DELTA)
        for name in indeterminate
        if name not in covered
    ]
    conf = max(floor, 1.0 - sum(degrader.delta for degrader in applied))
    return {
        "conf": round(conf, 2),
        "conf_degraders": [{"cause": degrader.cause, "delta": degrader.delta} for degrader in applied],
    }


def _fixed_confidence(value: float, reason: str) -> dict[str, Any]:
    """Return a rubric fixed-mode confidence, which replaces the degrader formula."""
    return {"conf": value, "conf_degraders": [], "conf_fixed": reason}


def _unavailable(reason: str) -> dict[str, Any]:
    """Mark an axis the rubric cannot score (⚪): no score, confidence 0, excluded from the health score."""
    return {
        "band": Band.UNAVAILABLE.value,
        "score": None,
        "conf": 0.0,
        "conf_degraders": [],
        "unavailable_reason": reason,
    }


def band_placement(red: dict[str, bool], green: dict[str, bool]) -> dict[str, Any]:
    """Pick the band of a band-only axis and place its score inside the band.

    Band: 🔴 when any 🔴 condition holds — the worst band wins when several band lines hold; else 🟢 when every
    🟢 clause holds; else 🟡. A clause on a missing metric does not hold. Score: 🟢 = 10 (every clause holds);
    🔴 and 🟡 = band anchor (🔴 1 · 🟡 4) + 1 per 🟢 clause held, capped at the band maximum (🔴 3 · 🟡 6).

    Args:
        red: 🔴 condition → whether it holds.
        green: 🟢 clause → whether it holds.

    Returns:
        ``{"band", "score", "clauses_held", "red_held"}``.

    Examples:
        >>> band_placement({"pct <40%": True}, {"median ≤7d": True, "pct ≥60%": False})["band"]
        '🔴'
        >>> band_placement({"pct <40%": True}, {"median ≤7d": True, "pct ≥60%": False})["score"]
        2
        >>> band_placement({}, {"a": True, "b": False})["score"], band_placement({}, {"a": True, "b": True})["score"]
        (5, 10)
    """
    red_held = [name for name, holds in red.items() if holds]
    held = [name for name, holds in green.items() if holds]
    if red_held:
        band = Band.RED
    elif len(held) == len(green):
        band = Band.GREEN
    else:
        band = Band.YELLOW
    anchor, cap = _BAND_RANGE[band]
    return {"band": band.value, "score": min(cap, anchor + len(held)), "clauses_held": held, "red_held": red_held}


def _checkpoint_band(met: int, green_min: int, yellow_min: int) -> str:
    """Band of a checkpoint axis from its met count.

    Examples:
        >>> _checkpoint_band(4, 4, 2), _checkpoint_band(3, 4, 2), _checkpoint_band(1, 4, 2)
        ('🟢', '🟡', '🔴')
    """
    if met >= green_min:
        return Band.GREEN.value
    return Band.YELLOW.value if met >= yellow_min else Band.RED.value


def _indeterminate(axis: dict[str, Any]) -> list[str]:
    """Checkpoint names in the ``indeterminate`` state."""
    return [name for name, cp in axis["checkpoints"].items() if cp["state"] == Checkpoint.INDETERMINATE.value]


# --- checkpoints --------------------------------------------------------------


def _cp(state: Checkpoint, why: str) -> dict[str, str]:
    """One checkpoint result: its state plus the evidence that decided it."""
    return {"state": state.value, "why": why}


def tally(checkpoints: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Score a checkpoint axis strictly: indeterminate counts as unmet, the upper bound credits it.

    Args:
        checkpoints: Checkpoint number → ``{"state", "why"}``.

    Returns:
        ``checkpoints`` plus ``met``, ``indeterminate``, ``applicable`` (not-applicable excluded) and
        ``score_strict`` / ``score_upper`` = ``floor(count / applicable × 10)``.

    Examples:
        >>> cps = {"1": _cp(Checkpoint.MET, ""), "2": _cp(Checkpoint.INDETERMINATE, ""), "3": _cp(Checkpoint.UNMET, "")}
        >>> {key: value for key, value in tally(cps).items() if key != "checkpoints"}
        {'met': 1, 'indeterminate': 1, 'applicable': 3, 'score_strict': 3, 'score_upper': 6}
    """
    states = [checkpoint["state"] for checkpoint in checkpoints.values()]
    applicable = sum(1 for state in states if state != Checkpoint.NOT_APPLICABLE.value)
    met = states.count(Checkpoint.MET.value)
    indeterminate = states.count(Checkpoint.INDETERMINATE.value)
    return {
        "checkpoints": checkpoints,
        "met": met,
        "indeterminate": indeterminate,
        "applicable": applicable,
        "score_strict": met * 10 // applicable if applicable else None,
        "score_upper": (met + indeterminate) * 10 // applicable if applicable else None,
    }


def _stem_is(*stems: str) -> Callable[[str], bool]:
    """Match lower-case file names whose stem (text before the first dot) is one of ``stems``."""
    return lambda name: name.split(".", 1)[0] in stems


def _policy_file(stem: str) -> Callable[[str], bool]:
    """Match a policy file (``security.md``, ``contributing.rst``); a bare ``security`` entry is usually a directory.

    Examples:
        >>> _policy_file("security")("security.md"), _policy_file("security")("security")
        (True, False)
    """
    return lambda name: name.split(".", 1)[0] == stem and "." in name


def _code_of_conduct(name: str) -> bool:
    """Match a code-of-conduct file in either spelling (``CODE_OF_CONDUCT.md``, ``CODE-OF-CONDUCT.md``).

    Examples:
        >>> _code_of_conduct("code-of-conduct.md"), _code_of_conduct("code_of_conduct"), _code_of_conduct("conduct.md")
        (True, False, False)
    """
    return "." in name and name.split(".", 1)[0] in _CODE_OF_CONDUCT_STEMS


def _listed(names: frozenset[str] | None, matches: Callable[[str], bool]) -> bool | None:
    """Tri-state listing lookup: ``True`` found, ``False`` listing known without it, ``None`` listing unknown."""
    return None if names is None else any(matches(name) for name in names)


def _presence(label: str, matches: Callable[[str], bool], *listings: frozenset[str] | None) -> dict[str, str]:
    """Presence checkpoint over listings: met when any lists the file, unmet only when every listing is known."""
    found = [_listed(listing, matches) for listing in listings]
    if any(result is True for result in found):
        return _cp(Checkpoint.MET, f"{label} listed")
    if all(result is False for result in found):
        return _cp(Checkpoint.UNMET, f"no {label}")
    return _cp(Checkpoint.INDETERMINATE, f"{label}: listing unavailable")


def _text_checkpoint(pattern: re.Pattern[str], text: str | None, absent: bool, label: str) -> dict[str, str]:
    """Content checkpoint: met/unmet from the text, unmet when the file is confirmed absent, else indeterminate."""
    if text is not None:
        hit = pattern.search(text)
        return _cp(Checkpoint.MET, f"{label}: '{hit.group(0)}'") if hit else _cp(Checkpoint.UNMET, f"no {label}")
    if absent:
        return _cp(Checkpoint.UNMET, f"{label}: file absent")
    return _cp(Checkpoint.INDETERMINATE, f"{label}: content not fetched")


def _github_names(view: DataView) -> frozenset[str] | None:
    """``.github/`` names; empty when the root listing proves there is no ``.github/``; ``None`` when unknown."""
    return _subdir_names(view, "github_dir", ".github")


def _docs_names(view: DataView) -> frozenset[str] | None:
    """``docs/`` names; empty when the root listing proves there is no ``docs/``; ``None`` when unknown.

    A CONTRIBUTING or SECURITY file kept only in ``docs/`` is undecidable while this is ``None``, never "absent".
    """
    return _subdir_names(view, "docs_dir", "docs")


def _subdir_names(view: DataView, record: str, directory: str) -> frozenset[str] | None:
    """Names of a listed subdirectory; empty when the root listing proves it absent; ``None`` when unknown."""
    names = view.names(record)
    if names is not None:
        return names
    root = view.names("root_contents")
    return frozenset() if root is not None and directory not in root else None


def text_dates(line: str) -> list[float]:
    """Epoch seconds of every rubric-format date in one line (invalid calendar dates skipped).

    Examples:
        >>> [datetime.fromtimestamp(s, tz=timezone.utc).date().isoformat() for s in text_dates("## [1.2] — 2026-09-30")]
        ['2026-09-30']
        >>> len(text_dates("v2.0 (5 March 2024)")), len(text_dates("March 2024")), text_dates("1.0.0")
        (2, 1, [])
    """
    triples: list[tuple[str, str, str]] = [(y, m, d) for y, m, d in _ISO_DATE_RE.findall(line)]
    triples += [(y, str(_MONTH_NUMBERS[mon[:3].lower()]), d) for d, mon, y in _DAY_MONTH_YEAR_RE.findall(line)]
    triples += [(y, str(_MONTH_NUMBERS[mon[:3].lower()]), d) for mon, d, y in _MONTH_DAY_YEAR_RE.findall(line)]
    triples += [(y, str(_MONTH_NUMBERS[mon[:3].lower()]), "1") for mon, y in _MONTH_YEAR_RE.findall(line)]
    stamps = []
    for year, month, day in triples:
        try:
            stamps.append(datetime(int(year), int(month), int(day), tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return stamps


def latest_changelog_date(outline: dict[str, Any], now: float) -> float | None:
    """Newest dated changelog entry: dates in headings, else in the first lines; future dates ignored.

    Examples:
        >>> headings = ["## [Unreleased]", "## [1.1] - 2024-02-01", "## [1.0] - 2023-01-01"]
        >>> outline = {"head": ["# Changelog"], "headings": headings}
        >>> datetime.fromtimestamp(latest_changelog_date(outline, 2e9), tz=timezone.utc).date().isoformat()
        '2024-02-01'
    """
    for lines in (outline.get("headings") or [], outline.get("head") or []):
        stamps = [stamp for line in lines for stamp in text_dates(str(line)) if stamp <= now + _DAY]
        if stamps:
            return max(stamps)
    return None


# --- Group A ----------------------------------------------------------------


def _first_response_days(node: dict[str, Any], connections: tuple[str, ...]) -> float | None:
    """Days from an item's creation to its first event by a human other than its author.

    Bot events never count: coverage, CLA and security apps comment on every PR within seconds,
    which made every PR look answered instantly.
    """
    created = parse_ts(node.get("createdAt"))
    author = _login(node.get("author"))
    stamps = [
        parse_ts(event.get("createdAt"))
        for connection in connections
        for event in _dig(node, connection, "nodes") or []
        if isinstance(event, dict) and _login(event.get("author")) != author and not _is_bot_actor(event.get("author"))
    ]
    stamps = [stamp for stamp in stamps if stamp is not None]
    if created is None or not stamps:
        return None
    return (min(stamps) - created) / _DAY


def _at_event_cap(node: dict[str, Any], connections: tuple[str, ...]) -> bool:
    """Tell whether any fetched event list of an item is full, so a human response may lie past the cap."""
    return any(len(_dig(node, connection, "nodes") or []) >= _EVENT_CAP for connection in connections)


def axis1_responsiveness(view: DataView) -> dict[str, Any]:
    """Axis 1 — first-response times for the sampled open issues and open/merged PRs.

    An unanswered issue younger than 7 days is right-censored: its 7-day outcome is still open, so it leaves both
    the numerator and the denominator of ``pct_responded_7d`` and ``pct_unresponded`` (``issues_too_young``).
    Counting it as unanswered deflated busy repositories, whose newest issues are hours old.
    """
    payload = view.data("responsiveness_gql")
    if not isinstance(payload, dict):
        return {"available": False, "reason": "responsiveness_gql missing"}
    issues = [node for node in _gql_nodes(payload, "issues") if isinstance(node, dict)]
    prs = [node for node in _gql_nodes(payload, "pullRequests") if isinstance(node, dict)]
    outcomes = [(_first_response_days(n, ("comments",)), view.age_days(n.get("createdAt")), n) for n in issues]
    issue_days = [days for days, _, _ in outcomes if days is not None]
    unanswered = [(age, node) for days, age, node in outcomes if days is None]
    too_young = sum(1 for age, _ in unanswered if age is not None and age < 7)
    eligible = len(issues) - too_young
    pr_outcomes = [(_first_response_days(n, ("reviews", "comments")), n) for n in prs]
    pr_days = [days for days, _ in pr_outcomes if days is not None]
    return {
        "available": True,
        "issues_sampled": len(issues),
        "issues_responded": len(issue_days),
        "issues_too_young": too_young,
        "issues_eligible": eligible,
        "median_issue_response_days": _median(issue_days),
        "pct_responded_7d": _pct(sum(1 for days in issue_days if days <= 7), eligible),
        "pct_unresponded": _pct(len(unanswered) - too_young, eligible),
        "issues_unresponded_at_event_cap": sum(1 for _, node in unanswered if _at_event_cap(node, ("comments",))),
        "prs_sampled": len(prs),
        "prs_responded": len(pr_days),
        "prs_unresponded_at_event_cap": sum(
            1 for days, node in pr_outcomes if days is None and _at_event_cap(node, ("reviews", "comments"))
        ),
        "median_pr_response_days": _median(pr_days),
        "note": "first 10 comments/reviews per item fetched (5 in older data); bot events never count; an item whose "
        "fetched events are all by its author or bots reads as unresponded (*_at_event_cap counts those whose list "
        "is full); unanswered issues younger than 7d are excluded from both percentages",
    }


def _commit_activity(view: DataView) -> dict[str, Any]:
    """Axis 2 commit recency and 30/90-day counts, with truncation lower-bound flags."""
    stamps = sorted((s for s in map(parse_ts, view.items("commits")) if s is not None), reverse=True)
    available = view.has("commits")
    truncated = view.partial("commits")
    oldest_age = (view.now - stamps[-1]) / _DAY if stamps else None
    return {
        "commits_available": available,
        "commits_sampled": len(stamps),
        "commits_truncated": truncated,
        "last_commit": datetime.fromtimestamp(stamps[0], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if stamps
        else None,
        "days_since_last_commit": (view.now - stamps[0]) / _DAY if stamps else None,
        "commits_30d": sum(1 for s in stamps if view.now - s <= 30 * _DAY) if available else None,
        "commits_90d": sum(1 for s in stamps if view.now - s <= 90 * _DAY) if available else None,
        "commits_30d_is_lower_bound": truncated and oldest_age is not None and oldest_age <= 30,
        "commits_90d_is_lower_bound": truncated and oldest_age is not None and oldest_age <= 90,
    }


def _release_activity(view: DataView) -> dict[str, Any]:
    """Axis 2 release recency and cadence (mean gap across the latest five releases)."""
    dated = sorted(
        ((parse_ts(r.get("published")), r.get("tag")) for r in view.items("releases") if isinstance(r, dict)),
        key=lambda pair: pair[0] or 0.0,
        reverse=True,
    )
    published = [stamp for stamp, _ in dated if stamp is not None][:5]
    gaps = [(newer - older) / _DAY for newer, older in pairwise(published)]
    latest_tag = dated[0][1] if dated else None
    return {
        "releases_count": len(dated),
        "days_since_last_release": (view.now - published[0]) / _DAY if published else None,
        "release_cadence_days": statistics.mean(gaps) if gaps else None,
        "latest_release_tag": latest_tag,
        "pre_release_tag": bool(latest_tag and _PRE_RELEASE_RE.search(str(latest_tag))),
    }


def _repo_name(view: DataView) -> str | None:
    """Repository name (``repo`` of ``owner/repo``) from the first record stamped with it; ``None`` when none is."""
    for record in view.records.values():
        slug = record.get("repo")
        if isinstance(slug, str) and "/" in slug:
            return slug.split("/", 1)[1] or None
    return None


def _named_abandon_re(name: str) -> re.Pattern[str]:
    """Abandonment status declared of the repository by its own name as the sentence subject ("Bleach is deprecated").

    The name must open a line (after blockquote, heading or emphasis marks) or a clause (after ``.!?:;`` or a dash);
    group ``statement`` holds the name and its status. A name that is also a common word appears inside other text
    ("the old black is deprecated") without being the subject.

    Examples:
        >>> _named_abandon_re("bleach").search("NOTE: 2023-01-23: Bleach is deprecated.").group("statement")
        'Bleach is deprecated'
        >>> bool(_named_abandon_re("rf-detr").search("RF-DETR is no longer maintained."))
        True
        >>> bool(_named_abandon_re("foo").search("foo-plus is deprecated"))
        False
        >>> bool(_named_abandon_re("black").search("Formatter. Note: the old black is deprecated"))
        False
    """
    return re.compile(
        rf"(?:{_ABANDON_LINE}(?:#+[ \t]+)?|[.!?:;—–][ \t]+)[*_`'\"\[(]*"
        rf"(?P<statement>{re.escape(name)}(?![\w.-])[*_`'\")\]]*\s+{_ABANDON_STATUS})",
        re.IGNORECASE | re.MULTILINE,
    )


@dataclass(frozen=True)
class _BannerLine:
    """One README line as the abandonment banner rule reads it.

    Attributes:
        content: The line's text with its markup stripped and its whitespace collapsed.
        level: Heading level (1 for the README title), ``0`` for any other line.
        warning: A warning sign marks the line.
    """

    content: str
    level: int
    warning: bool


def _banner_line(line: str, underline: str) -> _BannerLine | None:
    """Strip one README line down to the content a banner is read from; ``None`` for a list item or table row.

    Args:
        line: The README line.
        underline: The line below it; a ``===`` underline makes ``line`` a level-1 heading.

    Examples:
        >>> _banner_line('> <h1 align="center">**DEPRECATED** ⚠️</h1>', "")
        _BannerLine(content='DEPRECATED', level=1, warning=True)
        >>> _banner_line("### _Deprecated_: use `new_fn` instead ###", "")
        _BannerLine(content='Deprecated: use `new_fn` instead', level=3, warning=False)
        >>> _banner_line("- Deprecated", "") is None
        True
    """
    body = _LINE_START_RE.sub("", line, count=1)
    if _ITEM_LINE_RE.match(body):
        return None
    level = 1 if _SETEXT_TITLE_RE.fullmatch(underline) else 0
    heading = _ATX_HEADING_RE.fullmatch(body)
    if heading is not None:
        level, body = len(heading.group("marks")), heading.group("text")
    elif (html := _HTML_HEADING_RE.search(body)) is not None:
        level = int(html.group("level"))
    body = _ALERT_LABEL_RE.sub(" ", _HTML_TAG_RE.sub(" ", body))
    warning = _WARNING_SIGN_RE.search(body) is not None
    body = _EMPHASIS_RE.sub("", _WARNING_SIGN_RE.sub(" ", body))
    return _BannerLine(" ".join(body.split()), level, warning)


def _is_banner(line: _BannerLine) -> bool:
    """Tell whether a stripped README line declares the repository discontinued (``_BANNER_RE``, ``_TITLE_BANNER_RE``).

    The README title (level 1) and a line outside any heading fire on the status alone. A section heading (level 2 and
    below) names a section — "## Deprecated" lists deprecated features — so it fires only with a warning sign or a
    redirect.
    """
    status = _BANNER_RE.fullmatch(line.content)
    if status is not None:
        return line.level <= 1 or line.warning or status.group("redirect") is not None
    return line.level == 1 and _TITLE_BANNER_RE.fullmatch(line.content) is not None


def _banner_statements(head: str, cut: bool) -> list[str]:
    """README lines that are abandonment banners, stripped and lower-cased.

    Args:
        head: The README's first bytes.
        cut: The README continues past ``head``, so its last line is incomplete and never read: ``**Deprecated`` cut
            from ``**Deprecated APIs**`` would read as a status alone.

    Examples:
        >>> _banner_statements("# foo (DEPRECATED)\\n## Deprecated\\n", cut=False)
        ['# foo (deprecated)']
        >>> _banner_statements("# foo\\n**Deprecated", cut=True)
        []
    """
    lines = [line.rstrip("\r") for line in head.split("\n")]
    if cut:
        lines.pop()
    return [
        line.strip().lower()
        for line, underline in pairwise([*lines, ""])
        if (parsed := _banner_line(line, underline)) is not None and _is_banner(parsed)
    ]


def abandonment_statements(
    head: str | None, description: str | None, name: str | None, *, head_cut: bool = False
) -> list[str] | None:
    """Discontinuation statements about the repository itself, lower-cased and sorted; ``None`` when nothing was read.

    Args:
        head: The README's first 500 bytes, or ``None`` when the README was not fetched.
        description: The repository description, or ``None`` when ``repo_metadata`` carries none.
        name: The repository name, a subject the status may be declared of; ``None`` when unknown.
        head_cut: The README continues past ``head``; its last, incomplete line is read for no banner.

    Returns:
        Matched statements (``_ABANDON_RE`` on both texts, banner lines of the head, the repository name as sentence
        subject on both, and ``_ABANDON_DESCRIPTION_RE`` on the description), or ``None`` when neither text was read.

    Examples:
        >>> abandonment_statements("# foo\\nThe 1.x branch is no longer maintained.\\n", "A formatter", "foo")
        []
        >>> abandonment_statements(None, "DEPRECATED - use bar", None)
        ['deprecated - use']
        >>> abandonment_statements("**Deprecated:** Python 3.8 support was dropped.", "Unmaintained fork of bar", None)
        ['unmaintained fork of']
        >>> abandonment_statements("# DEPRECATED\\nDeprecated: use --new-flag instead of --old-flag\\n", None, "foo")
        ['# deprecated']
    """
    texts = [text for text in (head, description) if text is not None]
    if not texts:
        return None
    found = set(_matches(_ABANDON_RE, texts))
    if head is not None:
        found.update(_banner_statements(head, head_cut))
    if name:
        pattern = _named_abandon_re(name)
        found.update(match.group("statement").lower() for text in texts for match in pattern.finditer(text))
    if description is not None:
        found.update(_matches(_ABANDON_DESCRIPTION_RE, [description]))
    return sorted(found)


def axis2_maintenance(view: DataView) -> dict[str, Any]:
    """Axis 2 — commit and release activity plus the abandonment override inputs.

    The override reads GitHub's ``archived`` flag and statements that the repository itself is discontinued, in the
    description or the README's first 500 bytes (:func:`abandonment_statements`; a line the cut ends is read for no
    banner). ``archived`` is ``null`` when ``repo_metadata`` predates the field.
    """
    readme = view.text("readme_content")
    raw = readme.encode("utf-8") if readme is not None else None
    head = raw[:_ABANDON_HEAD_BYTES].decode("utf-8", errors="ignore") if raw is not None else None
    head_cut = raw is not None and len(raw) > _ABANDON_HEAD_BYTES
    meta = view.data("repo_metadata")
    description_checked = isinstance(meta, dict) and "description" in meta
    description = str(meta.get("description") or "") if description_checked else None
    archived = meta.get("archived") if isinstance(meta, dict) and isinstance(meta.get("archived"), bool) else None
    return {
        **_commit_activity(view),
        **_release_activity(view),
        "archived": archived,
        "abandonment_keywords": abandonment_statements(head, description, _repo_name(view), head_cut=head_cut),
        "readme_checked": head is not None,
        "description_checked": description_checked,
    }


@dataclass(frozen=True)
class WorkflowCoverage:
    """How much workflow content Axis 5 checkpoints 2–4 could read.

    Attributes:
        content: Concatenated workflow files, or ``None`` when none was fetched.
        listed: Workflow files in ``.github/workflows/``.
        fetched: Files whose content was read.
        complete: Every listed file was read, so a missing pattern is a real absence.
    """

    content: str | None
    listed: int
    fetched: int
    complete: bool


def _workflow_coverage(view: DataView) -> WorkflowCoverage:
    """Read the workflow-content record; older records without counts fall back to the listing and headers."""
    record = view.records.get("workflow_files") or {}
    raw = view.text("workflow_files")
    yaml_files = [n for n in view.items("workflows_list") if str(n).lower().endswith((".yml", ".yaml"))]
    fetched = int(record.get("fetched", len(_WORKFLOW_HEADER_RE.findall(raw)) if raw else 0))
    listed = int(record.get("listed", len(yaml_files)))
    complete = raw is not None and fetched >= listed and not record.get("partial", False)
    content = _WORKFLOW_HEADER_RE.sub("", raw) if raw is not None else None
    return WorkflowCoverage(content=content, listed=listed, fetched=fetched, complete=complete)


def _registry_ci(workflows: Any) -> dict[str, str]:
    """Axis 5 checkpoint 1 from the workflow registry alone, used when no ``.github/`` listing is known.

    The registry keeps entries whose file was deleted (``state: deleted``); those are not CI.
    """
    entries = workflows.get("workflows") if isinstance(workflows, dict) else None
    file_paths = [
        str(e.get("path"))
        for e in entries or []
        if isinstance(e, dict)
        and str(e.get("path", "")).startswith(".github/workflows/")
        and e.get("state") != "deleted"
    ]
    if file_paths:
        return _cp(Checkpoint.MET, f"{len(file_paths)} workflow files registered")
    if isinstance(workflows, dict) and not isinstance(entries, list) and (workflows.get("count") or 0) >= 1:
        return _cp(Checkpoint.MET, f"{workflows['count']} workflows registered (no paths in data)")
    if isinstance(workflows, dict):
        return _cp(Checkpoint.UNMET, "no workflow files")
    return _cp(Checkpoint.INDETERMINATE, "workflow data not fetched")


def _ci_present(view: DataView) -> dict[str, str]:
    """Axis 5 checkpoint 1 — workflow files exist (``.github/workflows/`` paths; dynamic workflows are not CI).

    The default-branch listing is the truth whenever it is known; the registry decides only without it. Only
    ``.yml``/``.yaml`` files are workflows: a directory holding just a README runs nothing.
    """
    listing = view.data("workflows_list")
    github = _github_names(view)
    if isinstance(listing, list) and listing:
        listed = sum(1 for name in listing if str(name).lower().endswith((".yml", ".yaml")))
        if listed:
            return _cp(Checkpoint.MET, f"{listed} workflow files")
        return _cp(Checkpoint.UNMET, "no .yml/.yaml file in .github/workflows")
    if github is not None:
        if "workflows" in github:
            return _cp(Checkpoint.MET, ".github/workflows listed")
        return _cp(Checkpoint.UNMET, "no workflow files")
    return _registry_ci(view.data("ci_workflows"))


def _code_scanning_default_setup(view: DataView) -> bool:
    """Tell whether GitHub's code-scanning default setup is active (a dynamic workflow, no YAML file)."""
    workflows = view.data("ci_workflows")
    entries = workflows.get("workflows") if isinstance(workflows, dict) else None
    return any(
        isinstance(e, dict)
        and str(e.get("path", "")).startswith("dynamic/github-code-scanning")
        and e.get("state") == "active"
        for e in entries or []
    )


def _content_checkpoint(
    pattern: re.Pattern[str], label: str, coverage: WorkflowCoverage, ci: dict[str, str]
) -> dict[str, str]:
    """Axis 5 checkpoints 2–4 — a step pattern in workflow content; unread files leave a miss indeterminate."""
    hits = _matches(pattern, [coverage.content]) if coverage.content is not None else []
    if hits:
        return _cp(Checkpoint.MET, f"{label}: {', '.join(hits)}")
    if coverage.complete:
        return _cp(Checkpoint.UNMET, f"no {label} step in {coverage.fetched} workflow files")
    if ci["state"] == Checkpoint.UNMET.value:
        return _cp(Checkpoint.UNMET, "no workflow files")
    return _cp(Checkpoint.INDETERMINATE, f"{label}: {coverage.fetched} of {coverage.listed} workflow files read")


def _default_branch_runs(view: DataView) -> list[dict[str, Any]]:
    """Return the fetched runs of a CI event (``_CI_EVENTS``) — the default branch's own CI.

    A run without an ``event`` field stays in: older DATA_FILEs fetched runs from every branch without it, and their
    runs are sampled unfiltered (see :func:`_runs_scope`).
    """
    runs = [run for run in view.items("ci_runs") if isinstance(run, dict)]
    return [run for run in runs if run.get("event") is None or run.get("event") in _CI_EVENTS]


def _runs_scope(view: DataView) -> str | None:
    """Which runs the checkpoint 5 sample is drawn from, in words; ``None`` without a runs record."""
    record = view.records.get("ci_runs")
    if record is None:
        return None
    branch = record.get("branch")
    if isinstance(branch, str):
        return f"default branch {branch}, {'/'.join(_CI_EVENTS)} events only"
    return "all branches and events (record fetched before default-branch scoping)"


def _run_sample(view: DataView) -> list[str]:
    """Conclusions of the checkpoint 5 sample: the newest 20 default-branch runs left after the exclusions.

    Exclusion comes first, then the slice, so pending, skipped or non-CI-event runs at the head of the list never shrink
    the sample below 20 while enough counted runs were fetched.

    Examples:
        >>> runs = [{"conclusion": c} for c in ("success", "skipped", None, "cancelled", "failure", "timed_out")]
        >>> runs += [{"conclusion": "success", "event": "workflow_run"}, {"conclusion": "failure", "event": "push"}]
        >>> _run_sample(DataView(records={"ci_runs": {"data": runs}}, now=0.0))
        ['success', 'failure', 'timed_out', 'failure']
    """
    conclusions = [str(run.get("conclusion") or "pending") for run in _default_branch_runs(view)]
    return [conclusion for conclusion in conclusions if conclusion not in _RUN_EXCLUDED][:_RUN_SAMPLE]


def _branch_unconfirmed(view: DataView) -> bool:
    """Tell whether the runs were fetched for a branch the data never confirmed as the default branch.

    oss:gh-scraper falls back to ``main`` when ``repo_metadata`` is missing; on a ``master`` repository that fetch
    returns no run. ``default_branch_status`` (fetched for the same branch, public) is then absent too.
    """
    record = view.records.get("ci_runs") or {}
    return isinstance(record.get("branch"), str) and not view.has("default_branch_status")


def _ci_health(view: DataView, sample: list[str]) -> dict[str, str]:
    """Axis 5 checkpoint 5 — ≥80% of the sampled runs succeeded (see :func:`_run_sample`).

    An empty run list is a measured "no run" only on a confirmed default branch; for a guessed branch it is undecided.
    """
    if not view.has("ci_runs"):
        return _cp(Checkpoint.INDETERMINATE, "runs not fetched")
    if not sample:
        if not view.items("ci_runs") and _branch_unconfirmed(view):
            branch = (view.records.get("ci_runs") or {}).get("branch")
            return _cp(Checkpoint.INDETERMINATE, f"no run on {branch}, a branch default_branch_status never confirmed")
        state = Checkpoint.INDETERMINATE if view.items("ci_runs") else Checkpoint.UNMET
        return _cp(state, "no counted run (pending, skipped, neutral, cancelled and non-CI-event runs excluded)")
    success = sample.count("success")
    state = Checkpoint.MET if success * 100 >= 80 * len(sample) else Checkpoint.UNMET
    return _cp(state, f"{success}/{len(sample)} counted runs succeeded")


def axis5_ci(view: DataView) -> dict[str, Any]:
    """Axis 5 — the five CI/CD checkpoints, scored strictly, plus the counts behind them."""
    coverage = _workflow_coverage(view)
    ci = _ci_present(view)
    sast = _content_checkpoint(_SAST_RE, "security scan", coverage, ci)
    if sast["state"] != Checkpoint.MET.value and _code_scanning_default_setup(view):
        sast = _cp(Checkpoint.MET, "code-scanning default setup active")
    sample = _run_sample(view)
    checkpoints = {
        "1": ci,
        "2": _content_checkpoint(_TESTS_RE, "test", coverage, ci),
        "3": _content_checkpoint(_LINT_RE, "lint", coverage, ci),
        "4": sast,
        "5": _ci_health(view, sample),
    }
    runs = [run for run in view.items("ci_runs") if isinstance(run, dict)]
    fetched = Counter(str(run.get("conclusion") or "pending") for run in runs)
    return {
        **tally(checkpoints),
        "workflow_files_listed": coverage.listed,
        "workflow_files_read": coverage.fetched,
        "runs_scope": _runs_scope(view),
        # one fetched page; below 20 counted runs out of a full page, older counted runs sit past it
        "runs_fetched": len(runs),
        "runs_event_excluded": len(runs) - len(_default_branch_runs(view)),
        "runs_sampled": len(sample),
        "run_conclusions": dict(sorted(fetched.items())),
        "ci_pass_rate_pct": _pct(sample.count("success"), len(sample)),
    }


def _calendar_days(now: float, stamp: float) -> int:
    """Whole UTC calendar days from ``stamp`` to ``now``.

    Changelog dates carry no time of day, so an entry dated 365 days before the scrape is 365 days old at any hour of
    ANALYSIS_NOW; measuring in seconds made it 365.x days old, and stale, at every instant but midnight.

    Examples:
        >>> _calendar_days(366 * 86400 + 13 * 3600, 86400.0)
        365
    """
    later = datetime.fromtimestamp(now, tz=timezone.utc).date()
    return (later - datetime.fromtimestamp(stamp, tz=timezone.utc).date()).days


def _version_tokens(text: str) -> list[str]:
    """Version numbers a changelog heading or release tag names, lower-cased, without a leading ``v``.

    Examples:
        >>> _version_tokens("## Version 26.10.0"), _version_tokens("## [v1.2.3] - 2024-01-05")
        (['26.10.0'], ['1.2.3'])
        >>> _version_tokens("21.12b0"), _version_tokens("1.0.0-rc.1.")
        (['21.12b0'], ['1.0.0-rc.1'])
        >>> _version_tokens("## Unreleased"), _version_tokens("## 2024-01-05")
        ([], [])
    """
    return [match.group(1).rstrip(".+-").lower() for match in _VERSION_RE.finditer(text)]


def _release_dates(view: DataView) -> dict[str, float]:
    """Publication time of each fetched release, keyed by the version its tag names (``v1.2.3`` → ``1.2.3``)."""
    dates: dict[str, float] = {}
    for release in view.items("releases"):
        published = parse_ts(release.get("published")) if isinstance(release, dict) else None
        versions = _version_tokens(str(release.get("tag") or "")) if published is not None else []
        if versions:
            dates[versions[0]] = max(published, dates.get(versions[0], published))
    return dates


def _released_heading(outline: dict[str, Any], releases: dict[str, float]) -> tuple[str, float] | None:
    """Newest release a changelog heading names by version: ``(version, published)``; ``None`` when no heading does.

    Examples:
        >>> outline = {"headings": ["## Unreleased", "## Version 26.1.0", "## Version 25.9.0"]}
        >>> _released_heading(outline, {"25.9.0": 9.0, "24.1.0": 5.0}), _released_heading(outline, {"24.1.0": 5.0})
        (('25.9.0', 9.0), None)
    """
    named = [
        (releases[version], version)
        for heading in outline.get("headings") or []
        for version in _version_tokens(str(heading))
        if version in releases
    ]
    if not named:
        return None
    published, version = max(named)
    return version, published


def _changelog_record_checkpoint(
    record: dict[str, Any], now: float, releases: dict[str, float] | None = None
) -> dict[str, str]:
    """Axis 6 checkpoint 4 from a fetched changelog outline.

    Ages are whole calendar days (:func:`_calendar_days`). A version heading that names a fetched release counts as an
    entry dated by that release's publication: changelogs such as psf/black's ``## Version 26.10.0`` carry no date, and
    stayed indeterminate while a release four days old proved the newest entry recent. A heading list cut at its cap
    (``truncated``) may hide the newest entry when the changelog runs oldest-first, so a truncated outline without a
    recent date stays indeterminate instead of reading as stale. When no heading carries a date at all, that is the
    cause stated first: an undated changelog (``## 25.1.0``) is not a truncation problem.

    Args:
        record: The ``changelog_headings`` record.
        now: ANALYSIS_NOW in epoch seconds.
        releases: Publication time per release version (:func:`_release_dates`); ``None`` when unknown.
    """
    source = record.get("source", "changelog")
    newest = latest_changelog_date(record["data"], now)
    age = None if newest is None else _calendar_days(now, newest)
    if _at_most(age, _CHANGELOG_MAX_AGE_DAYS):
        return _cp(Checkpoint.MET, f"{source}: newest dated entry {age}d old")
    released = _released_heading(record["data"], releases or {})
    if released is not None and _at_most(days := _calendar_days(now, released[1]), _CHANGELOG_MAX_AGE_DAYS):
        return _cp(Checkpoint.MET, f"{source}: version heading {released[0]} matches a release published {days}d ago")
    if age is None:
        cut = " (heading list truncated)" if record.get("truncated") else ""
        return _cp(Checkpoint.INDETERMINATE, f"{source}: no dated heading found{cut}")
    if record.get("truncated"):
        return _cp(Checkpoint.INDETERMINATE, f"{source}: heading list truncated, a newer entry may lie past the cap")
    return _cp(Checkpoint.UNMET, f"{source}: newest dated entry {age}d old")


def _changelog_checkpoint(view: DataView, root: frozenset[str] | None) -> dict[str, str]:
    """Axis 6 checkpoint 4 — a changelog whose newest dated entry is at most 365 days old.

    Listing evidence uses the assembler's predicates, so this checkpoint and ``datasets.not_present`` never
    disagree: only a listing with no changelog-like entry at all proves absence.
    """
    record = view.records.get("changelog_headings")
    if record is not None and isinstance(record.get("data"), dict):
        return _changelog_record_checkpoint(record, view.now, _release_dates(view))
    if root is None:
        return _cp(Checkpoint.INDETERMINATE, "root listing unavailable")
    if any(is_changelog_file(name) for name in root):
        return _cp(Checkpoint.INDETERMINATE, "changelog listed, content not fetched")
    if any(is_changelog_entry(name) for name in root):
        return _cp(Checkpoint.INDETERMINATE, "changelog-like entry listed (file or directory), content not fetched")
    return _cp(Checkpoint.UNMET, "no changelog file")


def _readme_checkpoints(view: DataView, root: frozenset[str] | None) -> dict[str, dict[str, str]]:
    """Axis 6 checkpoints 1–3 — README size, install section, usage section."""
    readme = view.text("readme_content")
    absent = readme is None and _listed(root, _stem_is("readme")) is False
    if readme is not None:
        size = len(readme.encode("utf-8"))
        first = _cp(Checkpoint.MET if size >= _README_MIN_BYTES else Checkpoint.UNMET, f"README {size} bytes")
    elif absent:
        first = _cp(Checkpoint.UNMET, "no README")
    else:
        first = _cp(Checkpoint.INDETERMINATE, "README not fetched")
    return {
        "1": first,
        "2": _text_checkpoint(_INSTALL_RE, readme, absent, "install section"),
        "3": _text_checkpoint(_USAGE_RE, readme, absent, "usage section"),
    }


def _contributing_absent(view: DataView, *listings: frozenset[str] | None) -> bool:
    """Tell whether CONTRIBUTING is confirmed absent: no fetched text and every listing known without it."""
    found = [_listed(listing, _policy_file("contributing")) for listing in listings]
    return view.text("contributing_text") is None and all(result is False for result in found)


def axis6_docs(view: DataView) -> dict[str, Any]:
    """Axis 6 — the nine documentation checkpoints, scored strictly."""
    root = view.names("root_contents")
    contributing = view.text("contributing_text")
    absent = _contributing_absent(view, root, _github_names(view), _docs_names(view))
    checkpoints = {
        **_readme_checkpoints(view, root),
        "4": _changelog_checkpoint(view, root),
        "5": _presence("docs/ directory", _stem_is("docs", "doc"), root),
        "6": _presence("examples/ directory", _stem_is("examples", "example"), root),
        "7": _text_checkpoint(_DEV_SETUP_RE, contributing, absent, "CONTRIBUTING dev setup"),
        "8": _text_checkpoint(_PR_PROCESS_RE, contributing, absent, "CONTRIBUTING PR process"),
        "9": _text_checkpoint(_STYLE_RE, contributing, absent, "CONTRIBUTING style guidance"),
    }
    source = (view.records.get("contributing_text") or {}).get("source")
    return {**tally(checkpoints), "contributing_source": source}


# --- Group B ----------------------------------------------------------------


def _zero_window(opened_30d: int | None, what: str) -> dict[str, str]:
    """Explain a rate left ``null`` because nothing was opened in the 30-day window."""
    return {"rate_null_reason": f"no {what} opened in 30d"} if opened_30d == 0 else {}


def _issue_health(view: DataView) -> dict[str, Any]:
    """Axis 4 issue staleness, age and 30-day close rate.

    A missing list or an empty 30-day window leaves the counts and ``close_rate`` ``null``: a ``0`` read as a measured
    rate below 0.4 and forced the axis 🔴 from absent data. The closed list is fetched for the 30-day closing window
    (older DATA_FILEs hold three years of closed issues in creation order), so its truncation means more in-window
    issues exist than were returned: the 30-day counts are then lower bounds. The list comes from GitHub search, which
    returns at most ``SEARCH_RESULT_CAP`` results, so a list of that size is truncated too — older DATA_FILEs asked for
    one more and never flagged it.
    """
    open_ok, closed_ok = view.has("open_issues"), view.has("closed_issues")
    open_issues = [i for i in view.items("open_issues") if isinstance(i, dict)]
    closed_issues = [i for i in view.items("closed_issues") if isinstance(i, dict)]
    ages = [age for age in (view.age_days(i.get("createdAt")) for i in open_issues) if age is not None]
    stale = sum(1 for i in open_issues if (view.age_days(i.get("updatedAt")) or 0) > 90)
    both = open_ok and closed_ok
    opened_30d = sum(1 for i in open_issues + closed_issues if view.within(i.get("createdAt"), 30)) if both else None
    closed_30d = sum(1 for i in closed_issues if view.within(i.get("closedAt"), 30)) if closed_ok else None
    closed_truncated = view.partial("closed_issues") or len(closed_issues) >= SEARCH_RESULT_CAP
    return {
        "open_available": open_ok,
        "open_count": len(open_issues) if open_ok else None,
        "open_truncated": view.partial("open_issues"),
        "stale_count": stale if open_ok else None,
        "stale_pct": _pct(stale, len(open_issues)),
        "median_open_age_days": _median(ages),
        "closed_available": closed_ok,
        "opened_30d": opened_30d,
        "closed_30d": closed_30d,
        "close_rate": _ratio(closed_30d, opened_30d),
        **_zero_window(opened_30d, "issues"),
        "closed_truncated": closed_truncated,
        "closed_30d_is_lower_bound": closed_truncated,
    }


def _human_prs(view: DataView, name: str) -> tuple[list[dict[str, Any]], bool]:
    """Non-bot PRs of one list, plus whether filtering was possible (older data carries no author)."""
    prs = [p for p in view.items(name) if isinstance(p, dict)]
    if prs and not all(isinstance(p.get("author"), dict) for p in prs):
        return prs, False
    return [p for p in prs if not is_bot(_login(p.get("author")), _dig(p, "author", "is_bot"))], True


def _pr_health(view: DataView) -> dict[str, Any]:
    """Axis 4 PR abandonment, 30-day merge rate and closed-without-merge ratio, bot PRs excluded.

    A missing list or an empty 30-day window leaves the counts and ``merge_rate`` ``null``, never ``0``. The closed list
    is fetched for the 30-day closing window, so its truncation means more in-window PRs exist than were returned: the
    30-day counts are then lower bounds (older DATA_FILEs hold a creation-ordered list, which a truncation leaves
    equally incomplete).
    """
    open_ok, closed_ok = view.has("open_prs"), view.has("closed_prs")
    open_prs, open_filtered = _human_prs(view, "open_prs")
    closed_prs, closed_filtered = _human_prs(view, "closed_prs")
    bots = len(view.items("open_prs")) + len(view.items("closed_prs")) - len(open_prs) - len(closed_prs)
    abandoned = sum(1 for p in open_prs if (view.age_days(p.get("updatedAt")) or 0) > 30)
    both = open_ok and closed_ok
    opened_30d = sum(1 for p in open_prs + closed_prs if view.within(p.get("createdAt"), 30)) if both else None
    merged_30d = sum(1 for p in closed_prs if view.within(p.get("mergedAt"), 30)) if closed_ok else None
    closed_30d = [p for p in closed_prs if view.within(p.get("closedAt"), 30)]
    unmerged_30d = sum(1 for p in closed_30d if not p.get("mergedAt"))
    closed_truncated = view.partial("closed_prs")
    return {
        "open_available": open_ok,
        "open_count": len(open_prs) if open_ok else None,
        "open_truncated": view.partial("open_prs"),
        "abandoned_count": abandoned if open_ok else None,
        "abandoned_pct": _pct(abandoned, len(open_prs)),
        "closed_available": closed_ok,
        "opened_30d": opened_30d,
        "merged_30d": merged_30d,
        "merge_rate": _ratio(merged_30d, opened_30d),
        **_zero_window(opened_30d, "PRs"),
        "closed_30d": len(closed_30d) if closed_ok else None,
        "closed_without_merge_ratio": _ratio(unmerged_30d, len(closed_30d)),
        "closed_truncated": closed_truncated,
        "closed_30d_is_lower_bound": closed_truncated,
        "bot_filter": "applied" if open_filtered and closed_filtered else "not applied: PR lists carry no author",
        "bots_excluded": bots,
    }


def _review_coverage(view: DataView) -> dict[str, Any]:
    """Axis 4 review coverage: non-bot merged PRs with an approving review by someone else."""
    payload = view.data("review_coverage_gql")
    if not isinstance(payload, dict):
        return {"available": False, "reason": "review_coverage_gql missing"}
    nodes = [n for n in _gql_nodes(payload, "pullRequests") if isinstance(n, dict)]
    human = [n for n in nodes if not _is_bot_actor(n.get("author"))]
    covered = sum(
        1
        for n in human
        if any(
            _login(r.get("author")) not in (None, _login(n.get("author"))) and not _is_bot_actor(r.get("author"))
            for r in _dig(n, "reviews", "nodes") or []
            if isinstance(r, dict)
        )
    )
    return {
        "available": True,
        "sampled": len(nodes),
        "non_bot": len(human),
        "approved_by_other": covered,
        "coverage_pct": _pct(covered, len(human)),
        "undefined": len(human) < 5,
    }


def axis4_issue_pr(view: DataView) -> dict[str, Any]:
    """Axis 4 — issue queue, PR queue, review coverage and the stalebot inflation signal."""
    workflows = view.data("ci_workflows")
    names = workflows.get("names") or [] if isinstance(workflows, dict) else []
    return {
        "issues": _issue_health(view),
        "prs": _pr_health(view),
        "review_coverage": _review_coverage(view),
        "stalebot_signal": _in_any("stale.yml", view.names("github_dir"))
        or any("stale" in str(name).lower() for name in names),
    }


def _contributors(view: DataView) -> list[tuple[str, list[int]]] | None:
    """Non-bot ``(login, weekly commit counts)`` from contributor stats; ``None`` when unavailable."""
    stats = view.data("contributor_stats")
    if not isinstance(stats, list):
        return None
    rows = []
    for entry in stats:
        # a null author (deleted or unlinked account) is skipped: collapsing all of them into one key invented a
        # single contributor out of many
        if not isinstance(entry, dict) or not entry.get("author") or is_bot(entry.get("author")):
            continue
        weeks = [int(w.get("c") or 0) for w in entry.get("weeks") or [] if isinstance(w, dict)]
        rows.append((str(entry["author"]), weeks))
    return rows


def _codeowners_users(text: str | None) -> list[str]:
    """Individual ``@user`` owners from CODEOWNERS (``@org/team`` entries and ``#`` comments excluded).

    Examples:
        >>> _codeowners_users("# lead @old\\n* @Alice @org/team  # backup @bob\\n")
        ['alice']
    """
    if text is None:
        return []
    owners = {
        token[1:].lower()
        for line in text.splitlines()
        for token in line.split("#", 1)[0].split()
        if token.startswith("@") and "/" not in token and len(token) > 1
    }
    return sorted(owners)


def _fetched_or_listed(
    view: DataView, record: str, label: str, matches: Callable[[str], bool], *listings: frozenset[str] | None
) -> dict[str, str]:
    """Presence checkpoint met by a fetched file record, else decided by the listings."""
    if view.has(record):
        source = (view.records.get(record) or {}).get("source")
        return _cp(Checkpoint.MET, f"{label} fetched" + (f" ({source})" if source else ""))
    return _presence(label, matches, *listings)


def _branch_protection(view: DataView) -> dict[str, str]:
    """Axis 7 checkpoint 6 — the default branch's ``protected`` flag (public), else the admin-only rules record."""
    status = view.data("default_branch_status")
    if isinstance(status, dict) and isinstance(status.get("protected"), bool):
        state = Checkpoint.MET if status["protected"] else Checkpoint.UNMET
        return _cp(state, f"default branch protected={status['protected']}")
    if view.has("branch_protection"):
        return _cp(Checkpoint.MET, "protection rules readable")
    return _cp(Checkpoint.INDETERMINATE, "protection flag not fetched")


def _active_maintainers(view: DataView, users: list[str]) -> tuple[dict[str, str], int | None]:
    """Axis 7 checkpoint 7 — share of CODEOWNERS users with commits in the last 13 weeks; N/A without both inputs."""
    contributors = _contributors(view)
    if not users:
        return _cp(Checkpoint.NOT_APPLICABLE, "no individual @owners in CODEOWNERS"), None
    if contributors is None:
        return _cp(Checkpoint.NOT_APPLICABLE, f"contributor stats {_stats_status(view)}"), None
    active = {login.lower() for login, weeks in contributors if sum(weeks[-13:]) >= 1}
    count = sum(1 for user in users if user in active)
    state = Checkpoint.MET if count * 2 >= len(users) else Checkpoint.UNMET
    return _cp(state, f"{count}/{len(users)} owners active"), count


def axis7_governance(view: DataView) -> dict[str, Any]:
    """Axis 7 — the seven governance checkpoints, scored strictly (checkpoint 7 may be not applicable)."""
    root = view.names("root_contents")
    github = _github_names(view)
    docs = _docs_names(view)
    users = _codeowners_users(view.text("codeowners_text"))
    maintainers, active = _active_maintainers(view, users)
    contributing = _policy_file("contributing")
    checkpoints = {
        "1": _presence("LICENSE", lambda name: name.startswith(("license", "licence", "copying")), root),
        "2": _fetched_or_listed(view, "security_text", "SECURITY", _policy_file("security"), root, github, docs),
        "3": _presence("CODE_OF_CONDUCT", _code_of_conduct, root, github),
        "4": _fetched_or_listed(view, "contributing_text", "CONTRIBUTING", contributing, root, github, docs),
        "5": _fetched_or_listed(view, "codeowners_text", "CODEOWNERS", _stem_is("codeowners"), root, github, docs),
        "6": _branch_protection(view),
        "7": maintainers,
    }
    return {
        **tally(checkpoints),
        "codeowners_users": users,
        "active_maintainers": active,
        "listed_maintainers": len(users),
        "active_ratio": active / len(users) if active is not None else None,
    }


def _alerts(view: DataView, name: str) -> dict[str, Any]:
    """Alert dataset status (``available`` / ``403`` / ``absent``) with open counts by severity."""
    if not view.has(name):
        return {"status": "absent"}
    if view.data(name) == "403":
        return {"status": "403"}
    alerts = [a for a in view.items(name) if isinstance(a, dict)]
    severity = Counter(
        str(_dig(a, "security_advisory", "severity") or _dig(a, "security_vulnerability", "severity") or "unknown")
        for a in alerts
    )
    return {
        "status": "available",
        "open_alerts": len(alerts),
        "by_severity": dict(sorted(severity.items())),
        "at_limit": view.partial(name),
    }


def axis8_security(view: DataView) -> dict[str, Any]:
    """Axis 8 — alert counts plus the three secondary signals and the 403 partial score, scored strictly."""
    root = view.names("root_contents")
    github = _github_names(view)
    security_md, depth = _security_md(view, root, github, _docs_names(view))
    secondary = {
        "dep_config": _dep_config(view, root, github),
        "dep_update_commits": _dep_commits(view),
        "security_md": security_md,
    }
    met = {key: cp["state"] == Checkpoint.MET.value for key, cp in secondary.items()}
    possible = {key: cp["state"] != Checkpoint.UNMET.value for key, cp in secondary.items()}
    return {
        "dependabot": _alerts(view, "dependabot_alerts"),
        "secret_scanning": _alerts(view, "secret_scanning_alerts"),
        "secondary": secondary,
        "security_md_depth": depth,
        "partial_score_strict": _partial_score(met),
        "partial_score_upper": _partial_score(possible),
    }


def _dep_config(view: DataView, root: frozenset[str] | None, github: frozenset[str] | None) -> dict[str, str]:
    """Axis 8 secondary — Dependabot or Renovate configured."""
    sources = [
        label
        for label, present in (
            ("dependabot_config record", view.has("dependabot_config")),
            (".github/dependabot config listed", any(_in_any(name, github) for name in DEPENDABOT_CONFIG_NAMES)),
            ("renovate config", any(_in_any(name, root, github) for name in _RENOVATE_FILES)),
        )
        if present
    ]
    if sources:
        return _cp(Checkpoint.MET, ", ".join(sources))
    if root is not None and github is not None:
        return _cp(Checkpoint.UNMET, "no Dependabot/Renovate config")
    return _cp(Checkpoint.INDETERMINATE, "listing unavailable")


def _dep_commits(view: DataView) -> dict[str, str]:
    """Axis 8 secondary — dependency-update commits in the last 90 days (from the last 50 commits)."""
    if not view.has("commits_50"):
        return _cp(Checkpoint.INDETERMINATE, "commits_50 not fetched")
    commits = [c for c in view.items("commits_50") if isinstance(c, dict)]
    count = sum(
        1 for c in commits if view.within(c.get("date"), 90) and _DEP_COMMIT_RE.match(str(c.get("message") or ""))
    )
    state = Checkpoint.MET if count else Checkpoint.UNMET
    return _cp(state, f"{count} dependency-update commits in last 90d ({len(commits)} commits sampled)")


def _security_md(view: DataView, *listings: frozenset[str] | None) -> tuple[dict[str, str], int | None]:
    """Axis 8 secondary — SECURITY.md depth (present 1, contact e-mail +1, response SLA +1) and its checkpoint.

    ``listings`` are the root, ``.github/`` and ``docs/`` names; absence needs every one of them known.
    """
    text = view.text("security_text")
    if text is not None:
        depth = 1 + bool(_EMAIL_RE.search(text)) + bool(_SLA_RE.search(text))
        return _cp(Checkpoint.MET, f"depth {depth}/3"), depth
    listed = [_listed(listing, _policy_file("security")) for listing in listings]
    if any(result is True for result in listed):
        return _cp(Checkpoint.MET, "SECURITY listed, content not fetched (depth ≥1)"), None
    if all(result is False for result in listed):
        return _cp(Checkpoint.UNMET, "no SECURITY policy"), 0
    return _cp(Checkpoint.INDETERMINATE, "listing unavailable"), None


def _partial_score(signals: dict[str, bool]) -> int:
    """Axis 8 partial score used when Dependabot alerts are 403: +4 config, +3 dep commits, +2 SECURITY, +1 all three.

    Examples:
        >>> _partial_score({"dep_config": True, "dep_update_commits": True, "security_md": True})
        10
        >>> _partial_score({"dep_config": False, "dep_update_commits": True, "security_md": True})
        5
    """
    points = 4 * signals["dep_config"] + 3 * signals["dep_update_commits"] + 2 * signals["security_md"]
    return min(10, points + (1 if all(signals.values()) else 0))


# --- Group C ----------------------------------------------------------------


def _bus_factor(counts: list[int]) -> int | None:
    """Fewest contributors whose 90-day commits exceed half the total; ``None`` when the total is zero.

    Examples:
        >>> _bus_factor([60, 30, 10])
        1
        >>> _bus_factor([40, 40, 20])
        2
        >>> _bus_factor([0, 0]) is None
        True
    """
    total = sum(counts)
    if total == 0:
        return None
    running = 0
    for index, count in enumerate(sorted(counts, reverse=True), start=1):
        running += count
        if running > total / 2:
            return index
    return len(counts)


def _commit_author_fallback(view: DataView) -> dict[str, Any]:
    """Axis 3 fallback while contributor stats are unavailable: bus factor from the last 50 commits.

    ``approx_bus_factor`` = distinct non-bot authors in ``commits_50``, capped at 3 (``null`` with none). The
    unresolved-author placeholder is skipped, since it may stand for several people.
    """
    # policy-sibling: skills/_shared/vitality-scoring-group-c.md (canonical), agents/repo-warden.md
    authors = {
        str(c["author"])
        for c in view.items("commits_50")
        if isinstance(c, dict) and c.get("author") and c["author"] != _UNKNOWN_AUTHOR and not is_bot(c["author"])
    }
    return {
        "commits_sampled": len(view.items("commits_50")),
        "unique_non_bot_authors": len(authors),
        "approx_bus_factor": min(len(authors), 3) if authors else None,
    }


def _pools(contributors: list[tuple[str, list[int]]]) -> tuple[set[str], set[str]]:
    """Contributors active in the last 26 weeks and in the 26 weeks before that."""
    recent = {login for login, weeks in contributors if sum(weeks[-26:]) >= 1}
    prior = {login for login, weeks in contributors if sum(weeks[-52:-26]) >= 1}
    return recent, prior


def _stats_status(view: DataView) -> str:
    """Contributor-stats state: ``available``, ``202_pending`` or ``absent``."""
    record = view.records.get("contributor_stats")
    if record is None:
        return "absent"
    return "available" if isinstance(record.get("data"), list) else "202_pending"


def axis3_contributors(view: DataView) -> dict[str, Any]:
    """Axis 3 — bus factor, concentration, retention, the commit-author fallback and an ``axis3_weeks`` summary."""
    contributors = _contributors(view)
    result: dict[str, Any] = {"stats_status": _stats_status(view), "fallback": _commit_author_fallback(view)}
    if contributors is None:
        return {**result, "axis3_weeks": None}
    counts = {login: sum(weeks[-13:]) for login, weeks in contributors}
    total = sum(counts.values())
    top_login, top_count = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
    active_q1 = {login for login, weeks in contributors if sum(weeks[-13:-7]) >= 1}
    active_both = {login for login, weeks in contributors if login in active_q1 and sum(weeks[-7:]) >= 1}
    recent, prior = _pools(contributors)
    return {
        **result,
        "contributors": len(contributors),
        "commits_90d_total": total,
        "all_90d_weeks_zero": total == 0,
        "bus_factor": _bus_factor(list(counts.values())),
        "top_contributor": top_login if total else None,
        "top_contributor_pct": _pct(top_count, total),
        "retention_active_q1": len(active_q1),
        "retention_active_both": len(active_both),
        "retention_pct": _pct(len(active_both), len(active_q1)),
        "axis3_weeks": {
            "contributors": len(contributors),
            "pool_recent_26w": len(recent),
            "pool_prior_26w": len(prior),
            "q1_active": len(active_q1),
            "q1q2_active": len(active_both),
        },
    }


def _pool_drift(view: DataView) -> dict[str, Any]:
    """Axis 9A — reviewer-pool shrinkage between the prior and recent 26-week windows."""
    contributors = _contributors(view)
    if contributors is None:
        return {"available": False, "reason": f"contributor stats {_stats_status(view)}"}
    recent, prior = _pools(contributors)
    if not prior:
        return {"available": False, "reason": "no contributors in prior window", "pool_recent": len(recent)}
    return {
        "available": True,
        "pool_recent": len(recent),
        "pool_prior": len(prior),
        "shrinkage_ratio": (len(prior) - len(recent)) / len(prior),
        "departed": len(prior - recent),
        "arrived": len(recent - prior),
    }


def _trend_ratio(recent: float | None, overall: float | None) -> float | None:
    """Ratio of the 30-day to the 90-day median merge time, from unrounded medians, itself unrounded.

    Rounding first turned PRs merged within minutes into ``0.0 / 0.0`` and lost the ratio. Equal zero medians are
    stable (1.0); a positive 30-day median over a zero 90-day median has no finite ratio (``None``).

    Examples:
        >>> _trend_ratio(0.006, 0.004), _trend_ratio(0.0, 0.0), _trend_ratio(1.0, 0.0), _trend_ratio(None, 1.0)
        (1.5, 1.0, None, None)
    """
    if recent is None or overall is None:
        return None
    if overall == 0:
        return 1.0 if recent == 0 else None
    return recent / overall


def _merge_trend(view: DataView) -> dict[str, Any]:
    """Axis 9B — median time-to-merge over the last 30 days vs the 90-day fetch."""
    prs = [p for p in view.items("merged_prs_90d") if isinstance(p, dict)]
    human = [p for p in prs if not is_bot(_login(p.get("author")), _dig(p, "author", "is_bot"))]
    spans = [(parse_ts(p.get("mergedAt")), parse_ts(p.get("createdAt"))) for p in human]
    spans = [(merged, created) for merged, created in spans if merged is not None and created is not None]
    all_days = [(merged - created) / _DAY for merged, created in spans]
    recent_days = [(merged - created) / _DAY for merged, created in spans if view.now - merged <= 30 * _DAY]
    median_30d = statistics.median(recent_days) if recent_days else None
    median_90d = statistics.median(all_days) if all_days else None
    oldest = min((merged for merged, _ in spans), default=None)
    return {
        "available": view.has("merged_prs_90d"),
        "merged_non_bot": len(spans),
        "bots_excluded": len(prs) - len(human),
        "merged_30d": len(recent_days),
        "median_30d_days": median_30d,
        "median_90d_days": median_90d,
        "trend_ratio": _trend_ratio(median_30d, median_90d),
        "truncated": view.partial("merged_prs_90d"),
        "window_days_covered": (view.now - oldest) / _DAY if oldest is not None else None,
    }


def _queue_depth(view: DataView) -> dict[str, Any]:
    """Axis 9C — 90th-percentile age of open issues."""
    ages = sorted(
        age
        for age in (view.age_days(i.get("createdAt")) for i in view.items("open_issues") if isinstance(i, dict))
        if age is not None
    )
    if not ages:
        return {"available": False, "reason": "no open issues" if view.has("open_issues") else "open_issues missing"}
    return {
        "available": True,
        "open_issues": len(ages),
        "p90_age_days": ages[int(len(ages) * 0.9)],
        "truncated": view.partial("open_issues"),
    }


def _automation_ratio(view: DataView) -> dict[str, Any]:
    """Axis 9D — share of the last 50 commits that are bot-authored dependency bumps."""
    commits = [c for c in view.items("commits_50") if isinstance(c, dict)]
    automated = sum(
        1 for c in commits if _AUTO_COMMIT_RE.match(str(c.get("message") or "")) and is_bot(c.get("author"))
    )
    return {
        "commits_sampled": len(commits),
        "automated": automated,
        "auto_ratio": automated / len(commits) if commits else None,
    }


def axis9_trajectory(view: DataView) -> dict[str, Any]:
    """Axis 9 — the four trajectory sub-signals; star velocity is never collected."""
    return {
        "9A_pool_drift": _pool_drift(view),
        "9B_merge_trend": _merge_trend(view),
        "9C_queue_depth": _queue_depth(view),
        "9D_automation": _automation_ratio(view),
        "star_velocity": {"available": False, "reason": "star data not collected"},
    }


# --- scoring per axis -----------------------------------------------------------
#
# Each scorer applies the rubric of ``skills/_shared/vitality-scoring-group-{a,b,c}.md`` and the Weights table of
# ``vitality-scoring.md`` to one axis's metrics. Degrader causes are worded in the field names printed beside them.


def score_axis1(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 1 — band, in-band score and confidence; ⚪ without an issue old enough or answered to judge.

    The sample-size degrader reads ``issues_eligible`` (sampled minus right-censored), the denominator the
    percentages actually use: 20 sampled with 16 censored left 4 items deciding the band at full confidence.
    """
    if not axis.get("available"):
        return _unavailable("responsiveness_gql missing")
    if not axis["issues_sampled"]:
        return _unavailable("no open issue sampled")
    if not axis["issues_eligible"]:
        return _unavailable("every sampled issue is unanswered and younger than 7d")
    issue, pr = axis["median_issue_response_days"], axis["median_pr_response_days"]
    responded, unresponded = axis["pct_responded_7d"], axis["pct_unresponded"]
    placement = band_placement(
        red={
            "median_issue_response >21d": _above(issue, 21),
            "pct_responded_7d <40%": _below(responded, 40),
            "pct_unresponded >60%": _above(unresponded, 60),
        },
        green={
            "median_issue_response ≤7d": _at_most(issue, 7),
            "median_pr_response ≤5d": _at_most(pr, 5),
            "pct_responded_7d ≥60%": _at_least(responded, 60),
        },
    )
    degraders = _degraders(
        (axis["issues_eligible"] < 5, Degrader("issues_eligible <5 (sampled minus issues_too_young)", 0.2)),
        (axis["prs_sampled"] < 5, Degrader("prs_sampled <5", 0.2)),
    )
    return {**placement, **confidence(_CONF_FLOOR["1"], degraders)}


def _abandonment_override(axis: dict[str, Any]) -> str | None:
    """Name the ⛔ abandonment override that applies to Axis 2, if any.

    Examples:
        >>> _abandonment_override({"archived": True, "abandonment_keywords": []})
        '⛔ repository archived'
        >>> _abandonment_override({"archived": None, "abandonment_keywords": None}) is None
        True
    """
    if axis["archived"] is True:
        return "⛔ repository archived"
    if axis["abandonment_keywords"]:
        return "⛔ self-declared discontinuation"
    return None


def score_axis2(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 2 — band, in-band score and confidence; the ⛔ abandonment override scores 0.

    The 🔴 condition excludes the maintenance-backport case (last commit >60d, ≥3 commits in 90d, release ≤180d), which
    the rubric upgrades to 🟡 — a release exactly 180 days old still upgrades, since the 🟡/🔴 boundary belongs to the
    better band. An empty commit list counts as zero commits. A missing commit list is ⚪ unless the override decides
    the axis: no clause is decidable, and defaulting to 🟡 scored absent data.
    """
    override = _abandonment_override(axis)
    if not axis["commits_available"] and override is None:
        return _unavailable("commits missing")
    days, recent = axis["days_since_last_commit"], axis["commits_30d"]
    release_days = axis["days_since_last_release"]
    backport = _above(days, 60) and _at_least(axis["commits_90d"], 3) and _at_most(release_days, _BACKPORT_RELEASE_DAYS)
    no_commits = axis["commits_available"] and not axis["commits_sampled"]
    stalled = no_commits or (_above(days, 60) and recent == 0)
    placement = band_placement(
        red={"last commit >60d and commits/30d = 0 (no backport upgrade)": stalled and not backport},
        green={"last commit ≤14d": _at_most(days, 14), "commits/30d ≥5": _at_least(recent, 5)},
    )
    if override is not None:
        placement = {**placement, "band": Band.RED.value, "score": 0, "override": override}
    degraders = _degraders(
        (not axis["commits_sampled"], Degrader("commits missing or empty", 0.3)),
        (
            axis["commits_30d_is_lower_bound"] or axis["commits_90d_is_lower_bound"],
            Degrader("commits_30d/90d lower bound (100-commit list truncated inside the window)", 0.15),
        ),
        (not axis["releases_count"], Degrader("no releases (releases missing or empty)", 0.1)),
    )
    return {**placement, **confidence(_CONF_FLOOR["2"], degraders)}


@dataclass(frozen=True)
class ContributorShape:
    """Axis 3 band inputs.

    Attributes:
        bus: Bus factor (fallback: approximated from commit authors); ``None`` when undefined.
        top: Top contributor's share of 90-day commits in percent; ``None`` when undefined.
        retention: Q1→Q2 retention in percent; ``None`` when undefined.
        dormant: No non-bot commit in the last 90 days (contributor stats available, every week zero).
    """

    bus: int | None
    top: float | None = None
    retention: float | None = None
    dormant: bool = False


def _axis3_placement(shape: ContributorShape, axis2_band: str) -> dict[str, Any]:
    """Axis 3 band and in-band score from bus factor, top-contributor share, retention and the Axis 2 band.

    A dormant 90-day window is decided data (nobody committed), so it is 🔴, never the 🟡 an all-undefined clause set
    would default to.
    """
    return band_placement(
        red={
            "bus factor 1 and Axis 2 🔴": shape.bus == 1 and axis2_band == Band.RED.value,
            "top contributor >75%": _above(shape.top, 75),
            "retention <30%": _below(shape.retention, 30),
            "no non-bot commit in 90d (all_90d_weeks_zero)": shape.dormant,
        },
        green={
            "bus factor ≥3": _at_least(shape.bus, 3),
            "top contributor ≤50%": _at_most(shape.top, 50),
            "retention ≥50%": _at_least(shape.retention, 50),
        },
    )


def score_axis3(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 3 — band, in-band score and confidence; commit-author fallback at fixed confidence when stats are absent."""
    axis2_band = axis_result("2", view)["band"]
    if axis["stats_status"] == "available":
        shape = ContributorShape(
            bus=axis["bus_factor"],
            top=axis["top_contributor_pct"],
            retention=axis["retention_pct"],
            dormant=axis["all_90d_weeks_zero"],
        )
        placement = _axis3_placement(shape, axis2_band)
        degraders = _degraders(
            (axis["contributors"] < 3, Degrader("contributors <3", 0.1)),
            (axis["all_90d_weeks_zero"], Degrader("all_90d_weeks_zero", 0.1)),
        )
        return {"axis2_band": axis2_band, **placement, **confidence(_CONF_FLOOR["3"], degraders)}
    approx = axis["fallback"]["approx_bus_factor"]
    if approx is None:
        reason = f"contributor stats {axis['stats_status']} and no commit author for the fallback"
        return {"axis2_band": axis2_band, **_unavailable(reason)}
    reason = f"contributor stats {axis['stats_status']}: commit-author fallback"
    placement = _axis3_placement(ContributorShape(bus=approx), axis2_band)
    return {"axis2_band": axis2_band, **placement, **_fixed_confidence(_CONF_AXIS3_FALLBACK, reason)}


def _axis4_degraders(issues: dict[str, Any], prs: dict[str, Any], coverage: dict[str, Any]) -> list[Degrader]:
    """Axis 4 listed degraders whose condition the data shows."""
    lists = {
        "open_issues": issues["open_available"],
        "closed_issues": issues["closed_available"],
        "open_prs": prs["open_available"],
        "closed_prs": prs["closed_available"],
    }
    available = bool(coverage.get("available"))
    return [Degrader(f"{name} missing", 0.2) for name, ok in lists.items() if not ok] + _degraders(
        (issues["open_truncated"], Degrader("open_issues truncated (501 returned)", 0.2)),
        (prs["open_truncated"], Degrader("open_prs truncated (201 returned)", 0.2)),
        (
            issues["closed_30d_is_lower_bound"] or prs["closed_30d_is_lower_bound"],
            Degrader("closed_30d_is_lower_bound (closed list truncated)", 0.15),
        ),
        (available and coverage["non_bot"] < 3, Degrader("review_coverage non_bot <3", 0.15)),
        (not available, Degrader("review_coverage_gql missing", 0.1)),
    )


def score_axis4(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 4 — worst-of band, in-band score and confidence; ⚪ when every issue and PR list is missing."""
    issues, prs, coverage = axis["issues"], axis["prs"], axis["review_coverage"]
    if not any(source[key] for source in (issues, prs) for key in ("open_available", "closed_available")):
        return _unavailable("open/closed issue and PR lists all missing")
    review = coverage["coverage_pct"] if coverage.get("available") and not coverage["undefined"] else None
    stale, close, merge = issues["stale_pct"], issues["close_rate"], prs["merge_rate"]
    placement = band_placement(
        red={
            "stale >30%": _above(stale, 30),
            "close_rate <0.4": _below(close, 0.4),
            "merge_rate <0.3": _below(merge, 0.3),
            "review_coverage <50%": _below(review, 50),
        },
        green={
            "stale ≤10%": _at_most(stale, 10),
            "close_rate ≥0.8": _at_least(close, 0.8),
            "merge_rate ≥0.7": _at_least(merge, 0.7),
            "review_coverage ≥80%": _at_least(review, 80),
        },
    )
    return {**placement, **confidence(_CONF_FLOOR["4"], _axis4_degraders(issues, prs, coverage))}


def _covers(*names: str) -> frozenset[str]:
    """Checkpoint names a degrader covers."""
    return frozenset(names)


def score_axis5(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 5 — strict checkpoint score, band from the met count (🟢 ≥4 · 🟡 2–3 · 🔴 ≤1) and confidence."""
    pending = set(_indeterminate(axis))
    content_gap = bool(pending & {"2", "3", "4"})
    degraders = _degraders(
        ("1" in pending, Degrader("ci_workflows missing and no listing shows workflows", 0.3, _covers("1"))),
        (
            content_gap and not axis["workflow_files_read"],
            Degrader("no workflow content read (workflow_files missing)", 0.2, _covers("2", "3", "4")),
        ),
        (
            content_gap and bool(axis["workflow_files_read"]),
            Degrader("workflow_files partial (some files unread)", 0.1, _covers("2", "3", "4")),
        ),
        (
            # no workflow files: checkpoint 5 is decided unmet, a short run list is no sample-size concern
            view.has("ci_runs") and axis["runs_sampled"] < 10 and axis["checkpoints"]["1"]["state"] != "unmet",
            Degrader("runs_sampled <10 (pass rate unstable)", 0.1, _covers("5")),
        ),
    )
    return {
        "band": _checkpoint_band(axis["met"], 4, 2),
        "score": axis["score_strict"],
        **confidence(_CONF_FLOOR["5"], degraders, _indeterminate(axis)),
    }


def score_axis6(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 6 — strict checkpoint score, band from the met count (🟢 ≥7 · 🟡 4–6 · 🔴 ≤3) and confidence."""
    pending = set(_indeterminate(axis))
    degraders = _degraders(
        (
            "1" in pending,
            Degrader("readme_content missing (README listed or root listing unknown)", 0.2, _covers("1", "2", "3")),
        ),
        (
            bool(pending & {"7", "8", "9"}),
            Degrader("contributing_text missing (CONTRIBUTING listed or listing unknown)", 0.1, _covers("7", "8", "9")),
        ),
    )
    return {
        "band": _checkpoint_band(axis["met"], 7, 4),
        "score": axis["score_strict"],
        **confidence(_CONF_FLOOR["6"], degraders, _indeterminate(axis)),
    }


def score_axis7(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 7 — strict checkpoint score, band from the met count (🟢 ≥5 · 🟡 3–4 · 🔴 ≤2) and confidence."""
    pending = set(_indeterminate(axis))
    listing_cps = {"1", "2", "3", "4", "5"}
    degraders = _degraders(
        (
            _github_names(view) is None and bool(pending & {"2", "3", "4", "5"}),
            Degrader("github_dir missing (.github/ listing unknown)", 0.1, _covers("2", "3", "4", "5")),
        ),
        (
            view.names("root_contents") is None and bool(pending & listing_cps),
            Degrader("root_contents missing", 0.05, frozenset(listing_cps)),
        ),
        ("6" in pending, Degrader("default_branch_status missing (protection flag not fetched)", 0.1, _covers("6"))),
        (
            bool(axis["codeowners_users"]) and _stats_status(view) != "available",
            Degrader("checkpoint 7 uncomputable: contributor stats unavailable, CODEOWNERS lists @users", 0.1),
        ),
    )
    return {
        "band": _checkpoint_band(axis["met"], 5, 3),
        "score": axis["score_strict"],
        **confidence(_CONF_FLOOR["7"], degraders, _indeterminate(axis)),
    }


def score_axis8(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 8 — alert bands when Dependabot alerts are available, else the partial score at fixed confidence.

    Each open secret-scanning alert counts as one high alert.

    Partial scoring: label 🟡 at 4 points or more, else 🔴 — never 🟢, the primary signal is missing. The score stays in
    its label's range (🟡 at most 6, 🔴 at most 3), so the partial points of 7–10 score 6. Uncapped, a token without
    alert access scored a repository 🟡 10, the same Health contribution as a verified 🟢 and more than an admin token's
    🟡 5 for one high alert; access now shows only in the fixed confidence 0.4 and the 🟡 ceiling.
    """
    dependabot, secret = axis["dependabot"], axis["secret_scanning"]
    if dependabot["status"] != "available":
        points = axis["partial_score_strict"]
        band = Band.YELLOW if points >= _PARTIAL_YELLOW_POINTS else Band.RED
        reason = f"dependabot_alerts {dependabot['status']}: partial scoring from secondary signals"
        score = min(points, _BAND_RANGE[band][1])
        return {"band": band.value, "score": score, **_fixed_confidence(_CONF_AXIS8_PARTIAL, reason)}
    severity = dependabot["by_severity"]
    critical = severity.get("critical", 0)
    high = severity.get("high", 0) + (secret.get("open_alerts", 0) if secret["status"] == "available" else 0)
    dep_config = axis["secondary"]["dep_config"]["state"]
    placement = band_placement(
        red={"≥1 critical alert": critical >= 1, "≥5 high alerts": high >= 5},
        green={
            "0 critical/high alerts": critical + high == 0,
            "dep-config present": dep_config == Checkpoint.MET.value,
        },
    )
    degraders = _degraders(
        (secret["status"] != "available", Degrader(f"secret_scanning_alerts {secret['status']}", 0.2)),
        (bool(dependabot["at_limit"]), Degrader("dependabot_alerts at 100-item limit", 0.15)),
    )
    pending = ["dep_config"] if dep_config == Checkpoint.INDETERMINATE.value else []
    return {"high_alerts_incl_secret": high, **placement, **confidence(_CONF_FLOOR["8"], degraders, pending)}


def _pool_drift_score(sub: dict[str, Any]) -> int | None:
    """9A score: 10 when the pool held or grew, 5 up to 30% shrinkage, 0 beyond or with nobody active."""
    if not sub["available"]:
        return None
    ratio = sub["shrinkage_ratio"]
    if ratio > 0.30 or not sub["pool_recent"]:
        return 0
    return 10 if ratio <= 0 else 5


def _merge_trend_score(sub: dict[str, Any]) -> int | None:
    """9B score: 10 when merges got no slower, 5 up to twice as slow, 0 beyond or without a merge in 30 days.

    No finite ratio despite merges in 30 days means a zero 90-day median against a positive 30-day one: slower.
    """
    if not sub["available"]:
        return None
    trend = sub["trend_ratio"]
    if not sub["merged_30d"] or trend is None or trend > 2.0:
        return 0
    return 10 if trend <= 1.0 else 5


def _queue_depth_score(sub: dict[str, Any]) -> int | None:
    """9C score: 10 up to a 30-day P90 issue age, 5 up to 180 days, 0 beyond; a boundary belongs to the better band.

    Examples:
        >>> [_queue_depth_score({"available": True, "p90_age_days": age}) for age in (30.0, 30.1, 180.0, 180.1)]
        [10, 5, 5, 0]
    """
    if not sub["available"]:
        return None
    age = sub["p90_age_days"]
    if age <= 30:
        return 10
    return 5 if age <= 180 else 0


def _automation_score(sub: dict[str, Any]) -> int | None:
    """9D score: 10 up to a 0.5 bot-bump share, 5 up to 0.9, 0 beyond; ``None`` without sampled commits.

    Examples:
        >>> [_automation_score({"auto_ratio": ratio}) for ratio in (0.5, 0.52, 0.9, 0.92, None)]
        [10, 5, 5, 0, None]
    """
    ratio = sub["auto_ratio"]
    if ratio is None:
        return None
    if ratio <= 0.5:
        return 10
    return 5 if ratio <= 0.9 else 0


def _sub_signal_scores(axis: dict[str, Any]) -> dict[str, int | None]:
    """Axis 9 sub-signal scores (10 🟢 · 5 🟡 · 0 🔴); ``None`` marks a ⚪ sub-signal."""
    return {
        "9A": _pool_drift_score(axis["9A_pool_drift"]),
        "9B": _merge_trend_score(axis["9B_merge_trend"]),
        "9C": _queue_depth_score(axis["9C_queue_depth"]),
        "9D": _automation_score(axis["9D_automation"]),
    }


def score_axis9(view: DataView, axis: dict[str, Any]) -> dict[str, Any]:
    """Axis 9 — mean of the available sub-signal scores, band 🟢 ≥7.5 · 🟡 ≥3.75 · 🔴 below, and confidence.

    A 🟢 axis scores the mean; a 🟡 or 🔴 mean is capped at the band maximum (🟡 6 · 🔴 3) like every other axis, so a
    🟡 trajectory never out-scores a 🟡 anywhere else (sub-signals 10/10/5/0 score 🟡 6, not 6.25).
    """
    subs = _sub_signal_scores(axis)
    available = [score for score in subs.values() if score is not None]
    if not available:
        return {"sub_scores": subs, **_unavailable("every trajectory sub-signal unavailable")}
    mean = statistics.mean(available)
    band = Band.GREEN if mean >= 7.5 else Band.YELLOW if mean >= 3.75 else Band.RED
    merge = axis["9B_merge_trend"]
    degraders = _degraders(
        (merge["available"] and merge["merged_non_bot"] < 5, Degrader("9B merged_non_bot <5", 0.2)),
        (_stats_status(view) != "available", Degrader(f"contributor_stats {_stats_status(view)} (9A unknown)", 0.2)),
        (axis["9D_automation"]["commits_sampled"] < 10, Degrader("9D commits_sampled <10", 0.1)),
        (bool(axis["9C_queue_depth"].get("truncated")), Degrader("9C open_issues truncated", 0.1)),
        (
            merge["truncated"] and _below(merge["window_days_covered"], 90),
            Degrader("9B truncated and window_days_covered <90", 0.1),
        ),
    )
    score = round(mean, 2) if band is Band.GREEN else min(round(mean, 2), _BAND_RANGE[band][1])
    return {"band": band.value, "score": score, "sub_scores": subs, **confidence(_CONF_FLOOR["9"], degraders)}


@dataclass(frozen=True)
class AxisSpec:
    """One axis: how its metrics are extracted and how the rubric scores them.

    Attributes:
        metrics: Builds the metrics the rubric reads from DATA_FILE.
        score: Applies the band, in-band score and confidence rules to those metrics.
    """

    metrics: Callable[[DataView], dict[str, Any]]
    score: Callable[[DataView, dict[str, Any]], dict[str, Any]]


#: Every scored axis.
AXES: dict[str, AxisSpec] = {
    "1": AxisSpec(axis1_responsiveness, score_axis1),
    "2": AxisSpec(axis2_maintenance, score_axis2),
    "3": AxisSpec(axis3_contributors, score_axis3),
    "4": AxisSpec(axis4_issue_pr, score_axis4),
    "5": AxisSpec(axis5_ci, score_axis5),
    "6": AxisSpec(axis6_docs, score_axis6),
    "7": AxisSpec(axis7_governance, score_axis7),
    "8": AxisSpec(axis8_security, score_axis8),
    "9": AxisSpec(axis9_trajectory, score_axis9),
}

#: Axes per group, in output order.
GROUP_AXES: dict[AxisGroup, tuple[str, ...]] = {
    AxisGroup.A: ("1", "2", "5", "6"),
    AxisGroup.B: ("4", "7", "8"),
    AxisGroup.C: ("3", "9"),
}


def axis_result(axis: str, view: DataView) -> dict[str, Any]:
    """Return one axis's band, score and confidence, followed by the metrics they were computed from.

    Args:
        axis: Axis number as a string (``"1"`` … ``"9"``).
        view: DATA_FILE records at ANALYSIS_NOW.

    Returns:
        Scoring keys (``band``, ``score``, ``conf``, ``conf_degraders``, ...) merged ahead of the axis metrics. The
        score is computed from the unrounded metrics; the metrics are then rounded for display (:func:`_display`).

    Examples:
        >>> result = axis_result("1", DataView(records={}, now=0.0))
        >>> result["band"], result["score"], result["conf"]
        ('⚪', None, 0.0)
    """
    spec = AXES[axis]
    metrics = spec.metrics(view)
    return {**spec.score(view, metrics), **_display(metrics)}


# --- I/O --------------------------------------------------------------------


def load_records(text: str) -> dict[str, dict[str, Any]]:
    """Parse DATA_FILE text into the last record per type, skipping blank and malformed lines.

    Examples:
        >>> sorted(load_records('{"type": "a", "data": 1}\\n\\n{"type": "a", "data": 2}\\n'))
        ['a']
        >>> load_records('{"type": "a", "data": 1}\\n{"type": "a", "data": 2}\\n')["a"]["data"]
        2
    """
    records: dict[str, dict[str, Any]] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"[vitality_extract] WARN: skipped malformed line {lineno}", file=sys.stderr)
            continue
        if isinstance(record, dict) and isinstance(record.get("type"), str):
            records[record["type"]] = record
    return records


def _analysis_now(records: dict[str, dict[str, Any]], override: int | None) -> float:
    """ANALYSIS_NOW: explicit override, else the first record timestamp, else the current time."""
    if override is not None:
        return float(override)
    for record in records.values():
        stamp = record.get("timestamp")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            return float(stamp)
    return time.time()


def _not_present(records: dict[str, dict[str, Any]], absent: list[str]) -> list[str]:
    """Pick the absent datasets whose file the repository listings prove does not exist.

    Group 2 swallows a 404, so a file-backed dataset (CODEOWNERS, SECURITY.md,
    ``.github/dependabot.yml``, ...) has no record when the repository simply lacks the
    file. That is complete data, not a fetch gap. The evidence rules are the assembler's own
    ``GROUP2_EVIDENCE`` and ``GROUP2_AMBIGUOUS`` so the two scripts never disagree. A
    dataset stays ``missing`` when the listings needed to prove absence are themselves
    unavailable, or when they are inconclusive (a bare ``changes`` entry may be a file).

    Args:
        records: Last record per type from DATA_FILE.
        absent: Wanted dataset names with no record.

    Returns:
        Names from ``absent`` whose file is proven absent from the repository.
    """
    listings = listings_of(records)
    if listings.root is None or (listings.github_dir is None and ".github" in listings.root):
        return []
    evidence, ambiguous = dict(GROUP2_EVIDENCE), dict(GROUP2_AMBIGUOUS)
    return [
        name
        for name in absent
        if name in evidence and not evidence[name](listings) and not (name in ambiguous and ambiguous[name](listings))
    ]


def extract(records: dict[str, dict[str, Any]], group: AxisGroup, analysis_now: int | None = None) -> dict[str, Any]:
    """Build the compact extraction for one axis group.

    Args:
        records: Last record per type from DATA_FILE.
        group: Axis group to extract.
        analysis_now: Optional ANALYSIS_NOW override in epoch seconds.

    Returns:
        ``{"group", "analysis_now", "datasets": {"used", "missing", "not_present", "optional_absent", "partial"},
        "axes": {...}}``. ``datasets`` is informational: every listed confidence degrader is already in the axes'
        ``conf``.
    """
    view = DataView(records=records, now=_analysis_now(records, analysis_now))
    wanted = GROUP_DATASETS[group]
    absent = [name for name in wanted if not view.has(name)]
    optional = [name for name in absent if name in _OPTIONAL_DATASETS]
    not_present = _not_present(records, [name for name in absent if name not in optional])
    return {
        "group": group.value,
        "analysis_now": int(view.now),
        "datasets": {
            "used": [name for name in wanted if view.has(name)],
            "missing": [name for name in absent if name not in not_present and name not in optional],
            "not_present": not_present,
            "optional_absent": optional,
            "partial": [name for name in wanted if view.partial(name)],
        },
        "axes": {axis: axis_result(axis, view) for axis in GROUP_AXES[group]},
    }


def main(argv: list[str] | None = None) -> int:
    """Print the compact per-axis extraction for one group.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success; ``1`` when DATA_FILE cannot be read.
    """
    parser = argparse.ArgumentParser(
        prog="vitality_extract.py",
        description="Print the per-axis metrics one oss:repo-warden group scores from the vitality DATA_FILE.",
    )
    parser.add_argument("--data-file", required=True, help="JSONL DATA_FILE written by oss:gh-scraper.")
    parser.add_argument("--group", required=True, choices=[g.value for g in AxisGroup], help="Axis group A, B or C.")
    parser.add_argument(
        "--analysis-now", type=int, default=None, help="Epoch seconds for time windows (default: data)."
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    try:
        with open(args.data_file, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"[vitality_extract] ERROR: cannot read DATA_FILE {args.data_file}: {exc}", file=sys.stderr)
        return 1
    result = extract(load_records(text), AxisGroup(args.group), args.analysis_now)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

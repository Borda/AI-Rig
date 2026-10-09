<!-- file: vitality-scoring-group-a.md — consumers: oss/agents/repo-warden.md -->

# Vitality Scoring Rubrics — Group A

> Axes 1, 2, 5, 6 — scored by oss:repo-warden AXIS_GROUP=A
>
> Split from `vitality-scoring.md` (see that file for Weights & Confidence Thresholds table, Advisory Signals, Implementation Status).

## Axes

### Axis 1 — Responsiveness

(CHAOSS top metric, most predictive of contributor attractiveness)

Data: GraphQL response from Group 1 (20 sampled issues + 20 sampled PRs).

Computation:

- Per issue: find first comment where `comment.author.login != issue.author.login`; `response_time = comment.createdAt − issue.createdAt` (fractional days). Issues with 0 non-author comments = "unresponded".
- Per PR: find earliest of (first non-author review) or (first non-author comment); `response_time = event.createdAt − pr.createdAt`.
- `median_issue_response_days` = median of response_times for issues with responses
- `median_pr_response_days` = median of response_times for PRs with non-author events
- `issues_eligible` = sampled issues minus unanswered issues younger than 7d (`issues_too_young` — their 7-day outcome is still open)
- `pct_responded_7d` = (count issues with response_time ≤7d) / `issues_eligible`
- `pct_unresponded` = count unanswered issues at least 7d old / same denominator
- Bot events never count; an item whose 10 fetched events are all by its author or bots reads as unresponded (`*_unresponded_at_event_cap` counts them — informational)

Score (§ In-Band Score Placement band rule — worst band wins):

- 🟢: median_issue_response ≤7d AND median_pr_response ≤5d AND pct_responded_7d ≥60%
- 🟡: otherwise — no 🔴 condition holds and some 🟢 clause fails (e.g. median_issue_response >7d to ≤21d, median_pr_response >5d, pct_responded_7d 40% to \<60%, no PR sampled)
- 🔴: median_issue_response >21d OR pct_responded_7d \<40% OR pct_unresponded >60% — beats 🟡 (fast answers to a few issues while the rest wait = 🔴)
- ⚪: `responsiveness_gql` missing, 0 issues sampled, or 0 eligible (every sampled issue unanswered and \<7d old) — cannot compute

### Axis 2 — Maintenance Activity

(velocity + cadence, most important single axis)

- Days since last commit; commits in last 30d and 90d
- Days since last release (if releases exist); release cadence = avg days between last 5 releases
- **Score** (B1 fix — no "stable/maintenance mode" false-positive loophole):
  - 🟢: last commit ≤14d AND commits/30d ≥5
  - 🟡: otherwise — last commit >14d to ≤60d OR commits/30d 1–4 OR maintenance backport (last commit >60d AND commits/90d ≥3 AND last release ≤180d — a release exactly 180d old still upgrades)
  - 🔴: last commit >60d AND commits/30d = 0, unless the backport clause holds (it upgrades to 🟡). Release recency alone never upgrades: only commits/90d ≥3 proves ongoing work. Zero commits (empty commit list) = 🔴; this covers "no commits for the entire quarter".
  - ⚪: `commits` dataset missing (no clause decidable) — unless the ⛔ override applies
  - ⛔ OVERRIDE 🔴, score 0 (abandonment signal) → 🔴 regardless of commit activity: repository archived (`repo_metadata.archived` true) OR the repository description / README first 500 bytes declare the repository itself discontinued. Case-insensitive extractor rules:
    - Sentence, README and description: `\bthis\s+(?:project|repo|repository|library|fork)\s+(?:is|has\s+been)\s+(?:now\s+)?(?:deprecated|unmaintained|archived|abandoned|discontinued|no\s+longer\s+(?:actively\s+|being\s+)?maintained|not\s+(?:being\s+)?(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))\b|\bno\s+longer\s+(?:actively\s+)?maintain(?:ing)?\s+this\s+(?:project|repo|repository|library|fork)\b` — a `this <project|repo|repository|library|fork>` subject with a status such as "is no longer maintained", "is not actively maintained", "is not maintained anymore" or "has been archived" (`package`, `module`, `tool` and `plugin` often name a sub-component and never count), or "no longer maintaining this project".
    - Repository name as the sentence subject, README and description: the same status after the repository's own name at a line start or after `.!?:;` or a dash ("Bleach is deprecated", "NOTE: 2023-01-23: Bleach is deprecated").
    - README banner, read line by line by its content, whatever its markup: indentation, blockquote and heading marks, HTML tags, a GitHub alert label (`[!WARNING]`), bold/italic marks and warning signs (⚠️ ⛔ ❗ ❌ 🚨 🚫 🛑) are stripped, then the rest must fully match `[\[(]?(?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))[\])]?[ \t]*(?:[.!]*|(?P<redirect>[.:!,;—–-]*[ \t]*(?:please[ \t]+)?(?:use|see|(?:moved|replaced|superseded)(?:[ \t]+(?:to|by))?)[ \t]+(?:\[[^\]]*\]\([^)\s]*\)|\x60[^\x60]+\x60|<?https?://\S+?>?|(?!(?:below|above|here|of)\b)[\w@][\w./@-]*)(?:[ \t]+instead)?[.!]*))` — the status alone, optionally in a tag bracket (`DEPRECATED`, `**DEPRECATED**`, `⚠️ Deprecated`, `<h1>DEPRECATED</h1>`, "No longer maintained."), or the status and a redirect to one replacement — a word, link, URL or code span (`\x60` is a backtick), optionally "instead" — that ends the line (`**DEPRECATED** use bar`, `> **Deprecated:** use bar instead.`, `DEPRECATED, please see bar`, "No longer maintained. Use bar instead."). The README title (a `#` heading, `<h1>` or a `===` underline) fires on the status alone (`# DEPRECATED`) and on the status beside the title text, `\S.*?(?:[ \t]+\((?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))\)|[ \t]+\[(?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))\]|[ \t]*[—–:][ \t]*(?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))|[ \t]+-[ \t]+(?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer))))[.!]*|\[(?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))\][ \t]+\S.*` (`# foo (DEPRECATED)`, `# foo — DEPRECATED`, `# foo [ARCHIVED]`, `# [DEPRECATED] foo`); a section heading (`##` and below) fires only with a warning sign (`## ⚠️ DEPRECATED`) or a redirect (`## Deprecated — use bar`). The line the 500-byte cut ends is incomplete and never read as a banner.
    - Description only: `^[ \t]*(?:\*{1,2}|_{1,2}|\[|\(|[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?[ \t]*)*(?:deprecated|unmaintained|archived|no\s+longer\s+(?:actively\s+)?maintained|not\s+(?:actively\s+maintained|maintained\s+any\s*(?:more|longer)))(?:(?:\*{1,2}|_{1,2}|\]|\)|[ \t]*[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?)*[ \t]*(?:[.!]?(?:\*{1,2}|_{1,2}|\]|\)|[ \t]*[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?)*[ \t]*$|[.!](?:\*{1,2}|_{1,2}|\]|\)|[ \t]*[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?)*(?=[ \t])|[.:!,;—–-]*(?:\*{1,2}|_{1,2}|\]|\)|[ \t]*[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?)*[ \t]*(?:please[ \t]+)?(?:use|see|moved|replaced|superseded)\b)|[\])]|(?:\*{1,2}|_{1,2}|\]|\)|[ \t]*[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?)*[ \t]*[:,;—–-]|(?:\*{1,2}|_{1,2}|\]|\)|[ \t]*[\U000026a0\U000026d4\U00002757\U0000274c\U0001f6a8\U0001f6ab\U0001f6d1]\U0000fe0f?)*[ \t]+(?:in[ \t]+favou?r[ \t]+of\b|(?:project|repo|repository|library|fork)(?:[ \t]+of\b|[ \t]*(?:[.!,;:—–-]|$))))` (a leading status also closed by a tag bracket `[UNMAINTAINED] foo`, followed by punctuation `Archived: old foo`, by "in favor of" `Deprecated in favor of bar`, or qualifying a repository subject that `of` or the clause's end follows `Unmaintained fork of foo` — never "Deprecated library finder").
    - Never overrides: a bare keyword or a statement about something else ("replacement for the deprecated foo", "Python 2 support is deprecated", "The 1.x branch is no longer maintained", "foo.bar: this module is deprecated"); a `:` clause naming another subject (`**Deprecated:** Python 3.8 support was dropped`); a status qualifying a noun (`_Deprecated_ options are listed below`, `[Deprecated] flags: --old`, `# Deprecated APIs`); a redirect naming the deprecated feature (`Deprecated: use --new-flag instead of --old-flag`, `` Deprecated: use `new_fn` instead of `old_fn` ``, `` Deprecated use of `foo()` now warns. ``, `**Deprecated:** see below for removed flags`, `Deprecated, see CHANGELOG for the migration of old APIs.`); a status followed by another sentence (`Archived. Releases before 2.0 live in the old repo.`); a list bullet (`- Deprecated`), a table row (`| Deprecated |`) or a bare section heading (`## Deprecated`); the repository's name inside other text ("the old black is deprecated").

### Axis 5 — CI/CD & Code Quality

(absent from prior design, repohealth scores CI/CD 35/100)

5 checkpoints:

1. CI workflows present — workflow files in `.github/workflows/` on the default branch (registry entries whose file is gone and `dynamic/*` workflows such as Pages or Dependency Graph are not CI)
2. Workflows run tests — content grep across every workflow file (case-insensitive): `pytest|jest|cargo test|go test|mvn test|rspec|phpunit|\b(?:npm|yarn|pnpm)(?:\s+-\S+(?:[ \t]+[^-\s]\S*)?)*?\s+(?:run\s+)?test(?:s|ing)?(?!\w)|\bnpm(?:\s+-\S+(?:[ \t]+[^-\s]\S*)?)*?\s+t(?![\w.:-])|\btox(?:\s+(?:run-parallel|run|r|p))?(?:\s+-\S+(?:[ \t]+[^-\s]\S*)?)*?\s+(?:(?:-e|--env)[\s=]*["']?(?:[\w.-]+,)*(?!(?:[\w.]+-)*(?:lint(?:ers?|ing)?|docs|typing|typecheck|mypy|type|fmt|format|style|build|publish|release|upload|fuzz|pre-commit|precommit|report)(?!\w))(?:(?:[\w.]+-)*py(?:py)?(?:\d[\w.]*|\$)|(?:py|pypy|ci)(?![\w-])|test(?:s|ing)?(?!\w))|(?:-f|--factors)[\s=]*["']?(?!(?:[\w.]+-)*(?:lint(?:ers?|ing)?|docs|typing|typecheck|mypy|type|fmt|format|style|build|publish|release|upload|fuzz|pre-commit|precommit|report)(?!\w))(?:(?:[\w.]+-)*py(?:py)?(?:\d[\w.]*|\$)|(?:py|pypy|ci)(?![\w-])|test(?:s|ing)?(?!\w))|(?:-m|--labels)[\s=]*["']?test(?:s|ing)?(?!\w))|\btox[-_]?env["']?[ \t]*[:=][ \t]*\[?[ \t]*(?:["']?[\w.-]+["']?[ \t]*,[ \t]*)*["']?(?!(?:[\w.]+-)*(?:lint(?:ers?|ing)?|docs|typing|typecheck|mypy|type|fmt|format|style|build|publish|release|upload|fuzz|pre-commit|precommit|report)(?!\w))(?:(?:[\w.]+-)*py(?:py)?(?:\d[\w.]*|\$)|(?:py|pypy|ci)(?![\w-])|test(?:s|ing)?(?!\w))|\bnox(?:\s+-\S+(?:[ \t]+[^-\s]\S*)?)*?\s+(?:-s|--sessions?|-t|--tags)[\s=]*["']?test(?:s|ing)?(?!\w)|\bhatch\s+(?:test\b|run\s+(?:[\w.-]+:)?test(?:s|ing)?(?!\w))|python[\d.]*\s+-m\s+unittest|\bmake\s+test(?:s|ing)?(?!\w)` — runners driven through tox, nox, hatch, unittest, make or a JS package manager count only in a test shape: a tox `-e` env list holding a test env — one with a versioned Python factor (`py311`, `ci-py311`, `ci-pypy3`, `py311-django42`, or a templated `ci-py$(…)`), the whole name `py`/`pypy`/`ci`, or `test`/`tests`/`testing` — and no non-test factor (`lint`, `linter`, `linters`, `linting`, `docs`, `typing`, `typecheck`, `mypy`, `type`, `fmt`, `format`, `style`, `build`, `publish`, `release`, `upload`, `fuzz`, `pre-commit`, `precommit`, `report`), with any tox flags before `-e`, with or without a value (`tox -p -e py311`, `tox -c tox.ini -e py311`, `tox run-parallel -e py311`); a tox 4 `-f` factor filter whose first factor is test-shaped (`tox -f py311`, `tox run -f py311 django42`); a tox `test` label (`tox -m test`); a `toxenv`/`TOXENV` assignment naming a test env (the matrix entry feeding `tox -e ${{ matrix.toxenv }}`); a `test*` nox session or tag after any nox flags (`nox -s tests`, `nox -p 3.11 -s tests`, `nox -t tests`); `hatch test` or `hatch run [env:]test`; `npm`/`yarn`/`pnpm` with any flags then `[run] test` (`pnpm -r test`, `npm --prefix web test`); `npm t`; `python -m unittest`; `make test`. Bare `tox`, other env names (`lint`, `fuzz`, `pylint`, `docs`, `py-lint`, `ci-lint`, `py311-lint`, `lint-py311`, `py39-mypy`, `py311-linters`, `py312-typecheck`, `py3-pre-commit`, `py311-coverage-report`, `testpypi-upload`), `npm run t` and `tox -e ${{ matrix.toxenv }}` without a test-env assignment never count. Accepted gap: `coverage` stays a test factor — `py311-coverage` usually runs the tests under coverage, so an env of that name that only combines reports is still credited
3. Workflows run linter/formatter — grep: `ruff|flake8|eslint|prettier|rubocop|golangci|black|mypy`
4. SAST or security scan present — grep: `codeql|semgrep|sonar|snyk|trivy|bandit|zizmor|osv-scanner|gitleaks|trufflehog|pip-audit|dependency-review-action|scorecard-action` (code, Actions-workflow, dependency, secret and supply-chain scanners: `actions/dependency-review-action`, `ossf/scorecard-action`) OR GitHub code-scanning default setup active (`dynamic/github-code-scanning` workflow, no YAML file; read from one registry page of 100 — a registry past it is `partial`, and an entry beyond the page goes unseen). A workflow *name* never counts — the `--- workflow: <name> ---` header is stripped before every content grep
5. Recent CI health — ≥80% of the newest 20 counted default-branch runs are "success" (Group 2 fetch `actions/runs?branch=<default>&status=completed&per_page=100`). Counted = completed default-branch runs of a CI event — `push`, `schedule`, `workflow_dispatch`, `merge_group` — except `skipped` and `neutral` (executed no job) and `cancelled` (superseded). Every other event stays out: pull-request events (`branch=` matches a run's head branch, so a pull request from a fork's own `main` comes back; its work-in-progress failures are not the project's CI health), `workflow_run` (runs in the default branch's context while reacting to pull requests, such as PR-comment bots) and `dynamic` (GitHub's own Dependabot or Copilot runs); pending runs never enter. Failures, `action_required`, `timed_out` and unknown conclusions all count. Exclusion comes before the slice, so excluded runs at the head of the list never shrink the sample. One page of 100 runs is fetched and never treated as a truncation: it holds the newest 20 counted runs unless more than 80 of them are excluded — then `runs_sampled` is below 20 with `runs_fetched` 100, older counted runs sit past the page, and the scorer says so in notes; only `runs_sampled` \<10 degrades confidence. No run at all is unmet — indeterminate when `default_branch_status` is missing, since the branch the runs were fetched for was then never confirmed. Older DATA_FILEs hold runs from every branch without `event`; they are sampled unfiltered (`runs_scope` says which)

- Checkpoint 1 decided by the default-branch `.github/` listing when known; the workflow registry decides only without it, and its `state: deleted` entries never count.
- If `ci_workflows` is missing and no listing shows workflows: checkpoint 1 indeterminate; confidence -0.3.
- Some workflow files unread (cap or fetch failure): a pattern found = met, not found = indeterminate; confidence -0.1. No content at all (`workflow_files` missing): checkpoints 2–4 indeterminate; confidence -0.2.
- If `runs_sampled` \<10: pass rate unstable; confidence -0.1 — not when checkpoint 1 is unmet (no workflow files: checkpoint 5 is decided, not unstable).

Score: floor(met / 5 × 10) → 0–10, strict (§ Unconfirmable Checkpoints); 🟢 ≥4/5 | 🟡 2–3/5 | 🔴 ≤1/5

### Axis 6 — Documentation

(content quality, not just presence; 9 checkpoints)

Note: CONTRIBUTING.md presence tracked in Axis 7 Governance, this axis scores content depth only.

9 checkpoints:

1. README present and ≥500 bytes (a threshold value meets the checkpoint)
2. README has install section (grep: `install|pip install|npm install|cargo add|brew install`)
3. README has usage/quickstart section (grep: `usage|quickstart|getting started|example`)
4. CHANGELOG present (CHANGELOG.md, CHANGES.md, HISTORY.md, NEWS.md) AND newest dated entry ≤365 days old, counted in UTC calendar days (dates carry no time, so an entry exactly 365 days old meets the checkpoint at any hour) — newest date among the changelog's headings (an undated `Unreleased` section on top is skipped), else in its first 10 lines; formats: YYYY-MM-DD, DD Month YYYY, Month DD YYYY, Month YYYY. A version heading naming a fetched release (`## Version 26.10.0` ↔ release tag `26.10.0`, a leading `v` ignored) counts as an entry dated by that release's publication. No dated heading and no version heading of a release published ≤365 days ago → indeterminate, reported as undated even when the heading list was also truncated. Heading list truncated at its cap with only old dates → indeterminate (an oldest-first changelog hides its newest entry past the cap). A bare `changes`/`changelog`/`history`/`news` root entry with no extension may be a fragment directory → indeterminate, never "absent"
5. docs/ or doc/ directory present
6. examples/ or example/ directory present
7. CONTRIBUTING (root, `.github/` or `docs/`, any extension) has dev-setup section (auto-fail if no CONTRIBUTING; grep: `setup|local.*install|dev.*env|getting started`). Absence needs every listing known: a root listing `docs/` without a fetched `docs_dir` listing leaves CONTRIBUTING undecided (indeterminate), never absent
8. CONTRIBUTING has PR/review process (grep: `pull.request|review.*process|merge.*process|workflow`)
9. CONTRIBUTING has code style or lint guidance (grep: `code.*style|lint|format|coding.*standard|ruff|mypy|eslint|prettier`)

Score: floor(met / 9 × 10), strict (§ Unconfirmable Checkpoints); 🟢 ≥7/9 | 🟡 4–6/9 | 🔴 ≤3/9

- README or CONTRIBUTING in `datasets.not_present` → its checkpoints are decided unmet, no confidence degrader.
- README listed (or root listing unknown) but `readme_content` missing: checkpoints 1–3 indeterminate; confidence -0.2. CONTRIBUTING listed but `contributing_text` missing: checkpoints 7–9 indeterminate; confidence -0.1.

## Extractor-Computed Values

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md, plugins/cc_oss/bin/vitality_extract.py -->

**Copy rule**: `bin/vitality_extract.py` computes every axis's `band`, `score`, `conf` and `conf_degraders` from these rules; the scorer copies `band` (as `label`), `score` and `conf` verbatim — notes may explain them, never change them.

## In-Band Score Placement

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md -->

**Band rule** (band-only axes): 🔴 when any 🔴 condition holds — the worst band wins when several band lines hold; else 🟢 when every 🟢 clause holds; else 🟡. A clause on a missing (`null`) metric does not hold. Ranges are half-open: a value on a boundary two band lines share belongs to the better band — every 🟢 clause includes its boundary (`≥` / `≤`), every 🔴 condition excludes it (`>` / `<`) (close_rate 0.8, pct_responded_7d 60%, stale 10%, last commit 14d, top contributor 50% → 🟢 clause holds; stale 30%, close_rate 0.4, median issue response 21d → 🟡). Thresholds compare unrounded metrics; printed metrics are rounded for display (a close_rate of 0.795 prints 0.8 and does not hold the ≥0.8 clause).

Axes 1, 2: 🟢 10; 🔴/🟡 score = band anchor (🔴 1 · 🟡 4) + 1 per 🟢-line clause that holds, capped at band max (🔴 3 · 🟡 6). Unavailable/`null` clause does not hold. Axis 1 clauses: median_issue_response ≤7d, median_pr_response ≤5d, pct_responded_7d ≥60%. Axis 2 clauses: last commit ≤14d, commits/30d ≥5. ⛔ abandonment override → 0. Axes 5, 6 keep their checkpoint formula.

**Confidence — listed degraders only** (Weights table in `vitality-scoring.md` plus the per-axis degraders above):

**Confidence formula**: conf = 1.0 − each listed degrader whose condition the data shows − 0.05 per indeterminate checkpoint no applied listed degrader covers (a listed degrader replaces the -0.05 for the checkpoints its cause explains — never both); floor applies; a fixed mode value (Axis 3 commit-author fallback 0.5, Axis 8 Dependabot alerts unavailable 0.4) replaces the formula.

Unlisted concern (small sample, odd value, extractor limit, unlisted truncation) → `notes`, never `conf`. `conf_degraders` names each applied degrader + delta.

## Unconfirmable Checkpoints

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/agents/repo-warden.md -->

Checkpoint states come from `vitality_extract.py`: `met` / `unmet` / `indeterminate` (input not fetched or unreadable) / `not_applicable`.

- Score strict — indeterminate counts as unmet; band from the met count.
- Never credit from a name, a midpoint, or a guess about unread content.
- Notes state the upper bound `floor((met + indeterminate) / applicable × 10)` when any checkpoint is indeterminate.
- Confidence per the Confidence formula: a listed degrader for the cause replaces the -0.05 for the checkpoints it explains — never both.

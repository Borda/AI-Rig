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
- `pct_responded_7d` = (count issues with response_time ≤7d) / (count all sampled issues)
- `pct_unresponded` = count issues with 0 non-author comments / count all sampled

Score:

- 🟢: median_issue_response \<7d AND median_pr_response \<5d AND pct_responded_7d ≥60%
- 🟡: median_issue_response ≤21d OR pct_responded_7d ≥40% (some responsiveness)
- 🔴: median_issue_response >21d OR pct_responded_7d \<40% OR pct_unresponded >60%
- ⚪: GraphQL 403 AND \<5 issues to sample (cannot compute)

### Axis 2 — Maintenance Activity

(velocity + cadence, most important single axis)

- Days since last commit; commits in last 30d and 90d
- Days since last release (if releases exist); release cadence = avg days between last 5 releases
- **Score** (B1 fix — no "stable/maintenance mode" false-positive loophole):
  - 🟢: last commit \<14d AND commits/30d ≥5
  - 🟡: last commit 14–60d OR commits/30d 1–4 OR (last commit >60d AND commits/90d ≥3 AND last release \<180d — genuine maintenance backports)
  - 🔴: last commit >60d AND commits/30d = 0 — regardless of release recency. Zero commits = 🔴. Release ≤180d only upgrades to 🟡 when commits/90d ≥3 proves ongoing work.
  - ALSO 🔴: commits/30d = 0 for >90d (no commits entire quarter)
  - ⛔ OVERRIDE 🔴 (abandonment signal): if repository description OR README first 500 bytes contains any of `abandoned`, `no longer maintained`, `deprecated`, `end-of-life`, `unmaintained`, `not maintained` (case-insensitive) → score 🔴 regardless of commit activity. Maintainer explicitly signaled discontinuation.

### Axis 5 — CI/CD & Code Quality

(absent from prior design, repohealth scores CI/CD 35/100)

5 checkpoints:

1. CI workflows present — `actions/workflows` count ≥1 OR `.github/workflows/` non-empty in directory listing
2. Workflows run tests — workflow file content grep (case-insensitive): `pytest|jest|cargo test|go test|npm test|mvn test|rspec|phpunit`
3. Workflows run linter/formatter — grep: `ruff|flake8|eslint|prettier|rubocop|golangci|black|mypy`
4. SAST or security scan present — grep: `codeql|semgrep|sonar|snyk|trivy|bandit` OR CodeQL action used
5. Recent CI health — ≥80% of last 20 run conclusions are "success" (from `actions/runs` response; N+1 detection: per_page=21, truncated if 21 returned)

- If `actions/workflows` returns 403: checkpoint 1 = ✗; confidence -0.3.
- If `.github/workflows/` absent from root-contents and API 403: checkpoint 1 = ✗ (no extra API call).
- If workflow content fetch fails (403 or decode error): checkpoints 2–4 = ✗; confidence -0.2.
- If \<10 run results: pass rate unstable; confidence -0.1.

Score: floor(met / 5 × 10) → 0–10; 🟢 ≥4/5 | 🟡 2–3/5 | 🔴 ≤1/5

### Axis 6 — Documentation

(content quality, not just presence; 9 checkpoints)

Note: CONTRIBUTING.md presence tracked in Axis 7 Governance, this axis scores content depth only.

9 checkpoints:

1. README present and >500 bytes
2. README has install section (grep: `install|pip install|npm install|cargo add|brew install`)
3. README has usage/quickstart section (grep: `usage|quickstart|getting started|example`)
4. CHANGELOG present (CHANGELOG.md, CHANGES.md, HISTORY.md, NEWS.md) AND most recent entry \<365d old (date in first 10 lines of file, formats: YYYY-MM-DD, DD Month YYYY, Month YYYY)
5. docs/ or doc/ directory present
6. examples/ or example/ directory present
7. CONTRIBUTING.md has dev-setup section (auto-fail if no CONTRIBUTING.md; grep: `setup|local.*install|dev.*env|getting started`)
8. CONTRIBUTING.md has PR/review process (grep: `pull.request|review.*process|merge.*process|workflow`)
9. CONTRIBUTING.md has code style or lint guidance (grep: `code.*style|lint|format|coding.*standard|ruff|mypy|eslint|prettier`)

Score: floor(met / 9 × 10); 🟢 ≥7/9 | 🟡 4–6/9 | 🔴 ≤3/9

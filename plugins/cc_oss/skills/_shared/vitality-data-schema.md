# Vitality Data Schemas

Reference schemas, oss:gh-scraper data files. Producer of record: `bin/assemble_vitality_data.py` (Group 1 records + coverage check); Group 2 records appended by `bin/fetch_gh_data_group2.py`. Consumer: `bin/vitality_extract.py` (per-axis metrics plus `band` / `score` / `conf` the scorer copies).

## JSONL Record Types (`DATA_FILE`)

Group 1 line: `{"type": "<dataset>", "repo": "<GH_OWNER>/<GH_REPO>", "timestamp": <ANALYSIS_NOW epoch int>, "records": N|null, "partial": true|false, "data": <raw_json>}`. Group 2 line: `{"type": "<dataset>", "data": <payload>}` (+ `source` / `branch` where noted).

Req = required: absent → envelope `status: "partial"` + listed in `missing_required`. `listed` = required only when the root, `.github/` or `docs/` listing shows the file (otherwise absence = file doesn't exist). `always` = required on every run (Group 2 canary). Cap = item count marking truncation (`partial: true`).

| `type` | Source | Req | Cap | Data shape |
| -- | -- | -- | -- | -- |
| `open_issues` | Group 1 | yes | 501 | array of issue objects |
| `closed_issues` | Group 1 (30d closing window) | yes | 1000 | array of issue objects, closed since CUTOFF_30D; GitHub search returns at most 1000, so a full list is read as truncated (older DATA_FILEs: last 3 years, creation order, cap 1001 never reached) |
| `open_prs` | Group 1 | yes | 201 | array of PR objects with `author{login, is_bot}` |
| `closed_prs` | Group 1 (30d closing window) | yes | 201 | array of PR objects with `author{login, is_bot}`, closed since CUTOFF_30D |
| `commits` | Group 1 | yes | 100 | array of ISO date strings |
| `releases` | Group 1 | yes | — | array of {tag, published, downloads} (latest 10) |
| `contributor_stats` | Group 1 | yes | — | array of {author, total, weeks}; `null` + `202_pending` while computing |
| `repo_metadata` | Group 1 | yes | — | {default_branch, description, archived, stargazers_count, forks_count, ...} |
| `ci_workflows` | Group 1 | yes | 100 | {count, total_count, names, workflows: [{name, path, state}]} — registry, includes `dynamic/*` and deleted-file workflows; one page of 100 (`per_page=100`); `partial` when `total_count` > `count` (older DATA_FILEs without `total_count`: `count` ≥100) |
| `dependabot_alerts` | Group 1 | no | 100 | array of alert objects or `"403"` |
| `secret_scanning_alerts` | Group 1 | no | 30 | array of alert objects or `"403"` |
| `fork_dates` | Group 1 | no | 100 | array of created_at strings |
| `merged_prs_90d` | Group 1 (Axis 9B) | yes | 201 | array of {number, createdAt, mergedAt, author{login, is_bot}} |
| `commits_50` | Group 1 (Axes 3 fallback, 8, 9D) | yes | — | array of {sha, message, author, date} |
| `responsiveness_gql` | Group 1 GraphQL | yes | — | 20 issues + 20 PRs, first 10 comments/reviews each, authors with `__typename` (`Bot` = automation) |
| `review_coverage_gql` | Group 1 GraphQL | yes | — | pullRequests nodes (30 most recently updated merged) |
| `root_contents` | Group 1 | yes | — | array of filename strings |
| `all_issues` | Group 1 | no | 200 | array of issue objects |
| `all_prs` | Group 1 | no | 100 | array of PR objects |
| `discussions` | Group 1 GraphQL | no | — | discussions nodes (fails when Discussions disabled) |
| `readme_content` | Group 2 | listed | — | decoded README text |
| `contributing_text` | Group 2 | listed | — | decoded CONTRIBUTING text; `source` = path (root, `.github/`, then `docs/`, any extension). Root lists `docs/` but `docs_dir` absent → absence inconclusive (neither required nor proof of absence) |
| `security_text` | Group 2 | listed | — | decoded SECURITY policy text; `source` = path (root, `.github/`, then `docs/`, any extension). Same `docs_dir` rule as `contributing_text` |
| `changelog_headings` | Group 2 | listed | — | {head: first 10 lines, headings: ATX/Setext heading lines (cap 300)}; `source`, `bytes`, `truncated`. Listed = a changelog-stem file with an extension; a bare `changes`/`changelog` entry is inconclusive (may be a fragment directory) — neither required nor proof of absence |
| `github_dir` | Group 2 | listed | — | array of `.github/` filename strings |
| `docs_dir` | Group 2 | listed | — | array of `docs/` filename strings; listed = the root lists `docs` |
| `codeowners_text` | Group 2 | listed | — | decoded CODEOWNERS text; `source` = path (`.github/CODEOWNERS`, root, then `docs/`). Same `docs_dir` rule as `contributing_text` |
| `default_branch_status` | Group 2 | always | — | {protected: bool} from the public branches API; `branch` = default branch. Absent = Group 2 did not run or used a wrong branch |
| `ci_runs` | Group 2 | always | — | array of {conclusion, name, event, head_branch}: one page of 100 completed runs of the default branch (`branch=`), newest first; `branch` = that branch, `records` = run count; the pass rate samples only `push`, `schedule`, `workflow_dispatch` and `merge_group` events. A full page is never flagged as truncation: it holds the newest 20 counted runs unless more than 80 are excluded (`runs_sampled` \<20 with `runs_fetched` 100 — the scorer notes it). Older DATA_FILEs: a Group 1 record of {conclusion, name} from every branch |
| `branch_protection` | Group 2 | no | — | protection rules object (admin only); `branch` = default branch. Absent for every non-admin token → extractor lists it under `datasets.optional_absent`, never `missing` |
| `workflows_list` | Group 2 | listed | — | array of `.github/workflows/` filename strings |
| `workflow_files` | Group 2 | listed | 50 | every `.yml`/`.yaml` workflow file, `--- workflow: <name> ---` headers; `listed`, `fetched`, `failed`, `partial` (unread files) |
| `dependabot_config` | Group 2 | listed | — | `.github/dependabot.yml` (else `.yaml`) contents-API object; `source` = path |

Rules (applied by `assemble_vitality_data.py`):

- Zero-byte Group 1 file = fetch failed → no record (missing). Valid empty array → record with `records: 0` (zero items is data)
- `dependabot_alerts` / `secret_scanning_alerts` failed fetch → `"data": "403"`, `records: 0` (contract, not a verified HTTP status — push access required)
- `contributor_stats` failed, `[]` or `{}` → `"data": null, "partial": true, "202_pending": true`
- `"partial": true` when item count reaches Cap
- `"records"` = list length; `0` for `"403"`; `null` for objects
- Re-run replaces Group 1 types, keeps last record of every other type; atomic replace

## Scores JSON Schema (`SCORES_FILE`)

```json
{
  "analysis_now": <ANALYSIS_NOW integer>,
  "today": "<TODAY string>",
  "axes": {
    "1": {"score": <AXIS1_SCORE>, "status": "<AXIS1_STATUS>", "conf": <AXIS1_CONF>, "signal": "<AXIS1_SIGNAL>"},
    "2": {"score": <AXIS2_SCORE>, "status": "<AXIS2_STATUS>", "conf": <AXIS2_CONF>, "signal": "<AXIS2_SIGNAL>"},
    "3": {"score": <AXIS3_SCORE>, "status": "<AXIS3_STATUS>", "conf": <AXIS3_CONF>, "signal": "<AXIS3_SIGNAL>"},
    "4": {"score": <AXIS4_SCORE>, "status": "<AXIS4_STATUS>", "conf": <AXIS4_CONF>, "signal": "<AXIS4_SIGNAL>"},
    "5": {"score": <AXIS5_SCORE>, "status": "<AXIS5_STATUS>", "conf": <AXIS5_CONF>, "signal": "<AXIS5_SIGNAL>"},
    "6": {"score": <AXIS6_SCORE>, "status": "<AXIS6_STATUS>", "conf": <AXIS6_CONF>, "signal": "<AXIS6_SIGNAL>"},
    "7": {"score": <AXIS7_SCORE>, "status": "<AXIS7_STATUS>", "conf": <AXIS7_CONF>, "signal": "<AXIS7_SIGNAL>"},
    "8": {"score": <AXIS8_SCORE>, "status": "<AXIS8_STATUS>", "conf": <AXIS8_CONF>, "signal": "<AXIS8_SIGNAL>"},
    "9": {"score": <AXIS9_SCORE>, "status": "<AXIS9_STATUS>", "conf": <AXIS9_CONF>, "signal": "<AXIS9_SIGNAL>"}
  },
  "overall_confidence": <OVERALL_CONFIDENCE float>,
  "health_score_pct": <HEALTH_SCORE_PCT integer>,
  "axis3_202_pending": <AXIS3_202_PENDING boolean>,
  "total_passes": <TOTAL_PASSES integer>,
  "confidence_history": "<CONFIDENCE_HISTORY string>"
}
```

Rules:

- Replace all `<VARIABLE>` placeholders with computed values
- ⚪ axes: score=-1, conf=-1, status="⚪", signal="unavailable — <reason>"

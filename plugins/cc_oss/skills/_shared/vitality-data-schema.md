# Vitality Data Schemas

Reference schemas, oss:gh-scraper data files.

## JSONL Record Types (`DATA_FILE`)

Each line: `{"type": "<dataset>", "repo": "<GH_OWNER>/<GH_REPO>", "timestamp": "<ANALYSIS_NOW>", "records": N, "partial": true|false, "data": <raw_json>}`

| `type` | Source | Data shape |
| -- | -- | -- |
| `open_issues` | Group 1: open issues | array of issue objects |
| `closed_issues` | Group 1: closed issues (3y window) | array of issue objects |
| `open_prs` | Group 1: open PRs | array of PR objects |
| `closed_prs` | Group 1: closed PRs | array of PR objects |
| `commits` | Group 1: recent commits | array of ISO date strings |
| `releases` | Group 1: releases | array of {tag, published, downloads} |
| `contributor_stats` | Group 1: contributor stats | array of {author, total, weeks} |
| `repo_metadata` | Group 1: repo metadata | {default_branch, stargazers_count, forks_count, ...} |
| `ci_workflows` | Group 1: CI workflows | {count, names} |
| `ci_runs` | Group 1: CI runs | array of {conclusion, name} |
| `dependabot_alerts` | Group 1: Dependabot | array of alert objects or string `"403"` |
| `fork_dates` | Group 1: fork velocity | array of created_at strings |
| `merged_prs_90d` | Group 1: Axis 9A | array of {number, createdAt, mergedAt, author} |
| `commits_50` | Group 1: Axis 9B | array of {sha, message, author, date} |
| `responsiveness_gql` | Group 1: GraphQL responsiveness | issues + PRs nodes |
| `review_coverage_gql` | Group 1: GraphQL review coverage | pullRequests nodes |
| `star_dates` | Group 2: star history | array of ISO date strings (180d window) |
| `readme_content` | Group 2: README file content | decoded string (base64-decoded from API) |
| `workflow_files` | Group 2: CI workflow file content | array of {name, content} (first 2 workflow files) |
| `root_contents` | Group 1: repo root file listing | array of filename strings (from `/contents` endpoint) |
| `github_dir` | Group 2: .github/ directory listing | array of filename strings |
| `codeowners_content` | Group 2: CODEOWNERS file content | decoded string (checks .github/CODEOWNERS then root CODEOWNERS) |
| `branch_protection` | Group 2: default branch protection | branch protection rules object (403 = push access required) |
| `dependabot_config` | Group 2: Dependabot config | dependabot.yml file object (404 = not configured) |

Rules:

- Skip datasets returning 403, persistent 202, or empty
- Set `"partial": true` when truncation detected (e.g. 501/201/1001 response count hits limit)
- Set `"records"` to item count in `data`
- After write: `echo "[repo-warden] raw data: N datasets → $DATA_FILE"`

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

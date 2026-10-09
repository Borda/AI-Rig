# Vitality Scoring Rubrics

Reference rubrics for oss:repo-warden (axis scoring + confidence). Split by axis group so each of 3 parallel scorer instances in oss:analyse vitality Step 2 reads only assigned group — not full rubric. Variables `$GH_OWNER`, `$GH_REPO`, fetched data sourced from DATA_FILE (written by oss:gh-scraper).

## Axis Group Index

Per-axis rubric text lives in group files, split by oss:repo-warden `AXIS_GROUP` assignment:

| Group file | Axes | Consumer |
| -- | -- | -- |
| `vitality-scoring-group-a.md` | 1 Responsiveness, 2 Maintenance Activity, 5 CI/CD & Code Quality, 6 Documentation | oss:repo-warden AXIS_GROUP=A |
| `vitality-scoring-group-b.md` | 4 Issue & PR Health, 7 Governance, 8 Security Posture | oss:repo-warden AXIS_GROUP=B |
| `vitality-scoring-group-c.md` | 3 Contributor Health, 9 Trajectory | oss:repo-warden AXIS_GROUP=C |
| `vitality-scoring-group-unassigned.md` | 10 Supply-Chain Integrity, 11 Ecosystem Criticality & Reach, 12 Dependency Health (Libyears), 13 Interface Stability & Community Engagement | none yet — see Implementation Status below |

This index file keeps only cross-cutting content shared across all groups: weight table (read by `assemble_vitality_scores.py`), advisory signals, data-fetching implementation status.

## Weights & Confidence Thresholds

Degrader causes are worded in `bin/vitality_extract.py` output fields (`missing`, `not_present`, `available: false`, `*_truncated`, `*_is_lower_bound`, checkpoint `indeterminate`), never in HTTP status codes the extractor cannot see. A file the listings prove absent (`datasets.not_present`) is decided data, never a degrader.

| Axis | Weight | Conf base | Key confidence degraders | Conf floor |
| -- | -- | -- | -- | -- |
| 1 Responsiveness | 0.10 | 1.0 | -0.2 `issues_eligible` \<5 (sampled minus `issues_too_young`, the percentages' denominator); -0.2 `prs_sampled` \<5; `responsiveness_gql` missing, 0 issues sampled or 0 eligible → ⚪ | 0.3 |
| 2 Maintenance activity | 0.08 | 1.0 | `commits` missing without the ⛔ override → ⚪; -0.3 `commits` missing or empty; -0.15 `commits_30d_is_lower_bound` or `commits_90d_is_lower_bound` (100-commit list truncated inside the window); -0.1 no releases (`releases` missing or empty) | 0.2 |
| 3 Contributor health | 0.10 | 1.0 | `stats_status` `202_pending`/`absent` → commit-author fallback at fixed 0.5 (no fallback author → ⚪); -0.1 `contributors` \<3; -0.1 `all_90d_weeks_zero` (band 🔴, stats may lag) | 0.4 |
| 4 Issue & PR health | 0.07 | 1.0 | -0.2 each of `open_issues` / `closed_issues` / `open_prs` / `closed_prs` missing (all four → ⚪); -0.2 `issues.open_truncated` (501 returned); -0.2 `prs.open_truncated` (201 returned); -0.15 `closed_30d_is_lower_bound` (closed list truncated); -0.15 `review_coverage.non_bot` \<3; -0.1 `review_coverage_gql` missing | 0.3 |
| 5 CI/CD & code quality | 0.07 | 1.0 | -0.3 checkpoint 1 indeterminate (`ci_workflows` missing, no listing shows workflows); -0.2 checkpoints 2–4 indeterminate, no workflow content read (`workflow_files` missing); -0.1 checkpoints 2–4 indeterminate, `workflow_files` partial; -0.1 `runs_sampled` \<10 (pass rate unstable; not when checkpoint 1 is unmet). A full page of 100 default-branch runs is never a truncation degrader; it holds the newest 20 counted runs unless >80 are excluded — `runs_sampled` \<20 with `runs_fetched` 100 → notes only | 0.4 |
| 6 Documentation | 0.05 | 1.0 | -0.2 checkpoints 1–3 indeterminate (`readme_content` missing while README listed or root listing unknown); -0.1 checkpoints 7–9 indeterminate (`contributing_text` missing while CONTRIBUTING listed or a listing unknown) | 0.5 |
| 7 Governance | 0.06 | 1.0 | -0.1 checkpoints 2–5 indeterminate, `github_dir` missing; -0.05 checkpoints 1–5 indeterminate, `root_contents` missing; -0.1 checkpoint 6 indeterminate (`default_branch_status` missing); -0.1 checkpoint 7 uncomputable (`stats_status` not `available` and CODEOWNERS lists @users). `branch_protection` absent (`optional_absent`, admin only) never degrades | 0.6 |
| 8 Security posture | 0.11 | 1.0 | `dependabot.status` not `available` (`403`/`absent`) → partial scoring at fixed 0.4; -0.2 `secret_scanning.status` not `available`; -0.15 `dependabot.at_limit` (100-item limit) | 0.2 |
| 9 Trajectory | 0.07 | 1.0 | -0.2 `9B.merged_non_bot` \<5 (TTM trend unstable); -0.2 `stats_status` not `available` (9A unknown); -0.1 `9D.commits_sampled` \<10; -0.1 `9C.truncated`; -0.1 `9B.truncated` AND `window_days_covered` \<90 | 0.3 |
| 10 Supply-chain integrity | 0.10 | 1.0 | -0.3 workflow content unreadable (checkpoints 1–3 indeterminate); -0.2 releases 403 or \<2 releases (checkpoints 4–5 unreliable); -0.1 \<3 releases sampled | 0.3 |
| 11 Ecosystem criticality | 0.06 | 1.0 | -0.4 dependents API unavailable AND downloads unavailable (score from stars/registry only); -0.2 dependent count pagination partial; -0.1 only one registry checked | 0.3 |
| 12 Dependency health | 0.08 | 1.0 | -0.2 if \<5 deps resolved; -0.2 if ≥1 registry API 429/503 (partial data); -0.3 transitive deps absent; -0.1 lock file absent | 0.3 |
| 13 Interface stability & community | 0.05 | 1.0 | -0.3 Group A ⚪ (no releases); -0.3 Group B ⚪ (repo \<6 months old); -0.2 contributor stats 202 with fallback; -0.1 Discussions API unavailable; -0.1 \<5 merged PRs in 90d | 0.3 |

## Unconfirmable Checkpoints

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/agents/repo-warden.md -->

Applies to the checkpoint axes (5, 6, 7) and the Axis 8 secondary signals. `bin/vitality_extract.py` gives every checkpoint one state — `met`, `unmet`, `indeterminate` (deciding input not fetched or unreadable) or `not_applicable` (dropped from the denominator) — with the evidence that decided it. The scorer uses that state; it never re-decides one.

- **Score strict**: `floor(met / applicable × 10)`; an indeterminate checkpoint counts as unmet. The band comes from the same met count.
- **Never credit what the data does not show**: no credit from a workflow or file *name*, no midpoint between the bounds, no inference about what an unread file probably contains.
- **State the upper bound** `floor((met + indeterminate) / applicable × 10)` in the notes whenever any checkpoint is indeterminate.
- **Lower confidence** per the Confidence formula below: a listed degrader for the cause replaces the -0.05 for the checkpoints it explains — never both.
- Axis 8 partial scoring (Dependabot unavailable) follows the same rule: points from `partial_score_strict`, capped at the label's band maximum (🟡 6, 🔴 3); note `partial_score_upper`.

## Extractor-Computed Values

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md, plugins/cc_oss/bin/vitality_extract.py -->

Two scorer models on identical data emit identical values because code computes them, not because both read the prose the same way. The rubric text stays the definition the extractor implements.

**Copy rule**: `bin/vitality_extract.py` computes every axis's `band`, `score`, `conf` and `conf_degraders` from these rules; the scorer copies `band` (as `label`), `score` and `conf` verbatim — notes may explain them, never change them.

## In-Band Score Placement

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md -->

Band-only axes — 1, 2, 3, 4, and 8 when Dependabot alerts are available — define a band but no number. Two scorers reading identical facts must still emit the same score, so band and number are fixed by rule, never by judgment:

**Band rule** (band-only axes): 🔴 when any 🔴 condition holds — the worst band wins when several band lines hold; else 🟢 when every 🟢 clause holds; else 🟡. A clause on a missing (`null`) metric does not hold. Ranges are half-open: a value on a boundary two band lines share belongs to the better band — every 🟢 clause includes its boundary (`≥` / `≤`), every 🔴 condition excludes it (`>` / `<`) (close_rate 0.8, pct_responded_7d 60%, stale 10%, last commit 14d, top contributor 50% → 🟢 clause holds; stale 30%, close_rate 0.4, median issue response 21d → 🟡). Thresholds compare unrounded metrics; printed metrics are rounded for display (a close_rate of 0.795 prints 0.8 and does not hold the ≥0.8 clause).

- **Score**: 🟢 = 10 (every 🟢 clause holds, so the band is full); 🔴 and 🟡 = band anchor + 1 per clause of the axis's 🟢 line that holds, capped at the band maximum.
- Anchor / maximum: 🔴 1 / 3 · 🟡 4 / 6 · 🟢 10.
- Clauses = the AND-terms of that axis's 🟢 line. A clause whose metric is unavailable, `null` or undefined does not hold.
- An override (⛔ abandonment on Axis 2) scores 0.
- Checkpoint axes (5, 6, 7) and Axis 8 with Dependabot unavailable keep their own formulas.
- Axis 9 scores its sub-signal mean: 🟢 keeps the mean; 🟡/🔴 cap it at the band maximum (🟡 6 · 🔴 3) — sub-signals 10/10/5/0 (mean 6.25) score 🟡 6.

## Confidence — Listed Degraders Only

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md -->

Confidence is computed, never judged, so two scorers on identical data report the same value:

**Confidence formula**: conf = 1.0 − each listed degrader whose condition the data shows − 0.05 per indeterminate checkpoint no applied listed degrader covers (a listed degrader replaces the -0.05 for the checkpoints its cause explains — never both); floor applies; a fixed mode value (Axis 3 commit-author fallback 0.5, Axis 8 Dependabot alerts unavailable 0.4) replaces the formula.

- Listed degraders = the Weights table above plus the axis's own confidence block (Axis 9).
- A concern no listed degrader covers — small sample, suspicious value, extractor limitation, truncation the table does not name — goes in `notes`, never into `conf`.
- `conf_degraders` names each degrader applied with its delta; `notes` may repeat them.

## Advisory Signals (non-scoring)

These signals appear in report annotations but don't affect numeric axis scores:

- **Version stability flag**: latest release tag matches `^0\.` or contains `alpha`/`beta`/`rc` (case-insensitive) → append `⚠ pre-release stability` flag to report header. Pre-release APIs may break without SemVer guarantee.
- **Stalebot inflation warning**: see Axis 4 close rate definition. Surfaced as report annotation, not score degrader.

## Implementation Status — Data Fetching Requirements

Axes 1–9 and 13 Group A use data already fetched by `oss:gh-scraper`. Axes 10–13 require new gh-scraper fetch groups. Until implemented, these axes score ⚪, excluded from weighted Health Score.

### gh-scraper changes required per axis

| Axis | New fetch(es) required | API / endpoint |
| -- | -- | -- |
| 10 Supply-chain integrity | Full workflow YAML content (not just list); release assets list | `GET /repos/{o}/{r}/contents/.github/workflows/{file}` per file; `GET /repos/{o}/{r}/releases?per_page=10` → `assets[].name` |
| 11 Ecosystem criticality | Dependent repo count; package registry weekly downloads | `GET https://github.com/{o}/{r}/network/dependents` HTML parse; PyPI `pypistats.org/api/packages/{n}/recent`; npm `api.npmjs.org/downloads/point/last-week/{n}` |
| 12 Dependency health | Package manifest files content; per-dep version dates from registry | `GET /repos/{o}/{r}/contents/{manifest_file}` for each manifest; PyPI `pypi.org/pypi/{n}/json`; npm `registry.npmjs.org/{n}/{v}`; crates.io `crates.io/api/v1/crates/{n}/versions` |
| 13 Group B community | GitHub Discussions count (single GraphQL field) | `graphql: repository { discussions(first:1) { totalCount } }` |

### Remaining Phase 2 signals (not yet assigned to an axis)

| Signal | Priority | Rationale |
| -- | -- | -- |
| OSV/CVE direct query (Axis 8 supplement) | Critical | Dependabot 403 common; OSV.dev `api.osv.dev/v1/query` fully public; supplements partial scoring in Axis 8 |
| Organizational diversity / Elephant Factor (Axis 3 supplement) | High | CNCF graduation requirement; single-org repos fail sustainable ownership; requires `GET /users/{login}` for top contributors → `company` field |
| Fuzzing enrollment (Axis 5 supplement) | Medium | OpenSSF Scorecard Fuzzing check; high value for parser/codec/network repos; OSS-Fuzz membership list publicly queryable |
| SLSA build level (Axis 10 supplement) | Medium | Build provenance level L1–L3; data already partially in Axis 10 checkpoint 4 (signed-releases) — needs SLSA-specific parsing |

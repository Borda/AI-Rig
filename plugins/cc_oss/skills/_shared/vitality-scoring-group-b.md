<!-- file: vitality-scoring-group-b.md — consumers: oss/agents/repo-warden.md -->

# Vitality Scoring Rubrics — Group B

> Axes 4, 7, 8 — scored by oss:repo-warden AXIS_GROUP=B
>
> Split from `vitality-scoring.md` (see that file for Weights & Confidence Thresholds table, Advisory Signals, Implementation Status).

## Axes

### Axis 4 — Issue & PR Health

(queue hygiene + code review quality, merged from old Axes 1+2)

Issue signals (from open/closed issue lists):

- stale % = open issues with no update >90d / total open
- close rate = closed last 30d / opened last 30d — `null` (never 0) when either issue list is missing or no issue opened in 30d (`rate_null_reason`); if stale-bot config present (`.github/stale.yml` or `stale` in workflow names) AND close_rate ≥2.0, flag "⚠ potential stalebot inflation" in report — stalebot auto-closes inflate close_rate without resolution
- median open issue age (days)
- closed list truncated → `closed_30d_is_lower_bound` (30-day counts are lower bounds). Both closed lists are fetched for the 30-day closing window (`closed:>=CUTOFF_30D`) through GitHub search, so they truncate only when 1000 or more issues or more than 200 PRs closed in 30 days — search returns at most 1000 results, so a closed-issue list of exactly 1000 is read as truncated; older DATA_FILEs hold closed issues from the last 3 years in creation order, where a truncated list can miss an old issue closed this month

PR signals (from open/closed PR lists; filter bot PRs):

- merge rate = merged last 30d / opened last 30d (bot-filtered) — `null` (never 0) when either PR list is missing or no PR opened in 30d
- abandoned % = open PRs with no update >30d / total open
- closed-without-merge ratio = closed PRs with mergedAt=null / total closed last 30d

Code-review coverage (from GraphQL, last 30 merged PRs; filter bot PRs):

- `review_coverage` = count(PRs with ≥1 non-author approving review) / count(all non-bot merged PRs sampled)
- "undefined" if \<5 non-bot merged PRs in sample

Score (worst-of composite — any 🔴 dimension → axis 🔴; § In-Band Score Placement band rule):

- 🟢: stale ≤10% AND close_rate ≥0.8 AND merge_rate ≥0.7 AND review_coverage ≥80%
- 🟡: otherwise — stale >10% to ≤30% OR close_rate 0.4 to \<0.8 OR merge_rate 0.3 to \<0.7 OR review_coverage 50% to \<80% OR review_coverage undefined OR a `null` rate
- 🔴: stale >30% OR close_rate \<0.4 OR merge_rate \<0.3 OR review_coverage \<50% (undefined coverage and `null` rates never trigger 🔴)
- ⚪: open/closed issue and PR lists all missing

### Axis 7 — Governance

(7 checkpoints, weight increased above Documentation per H1 fix)

1. LICENSE present (root)
2. SECURITY.md present (root, .github/ or docs/, any extension) — absence needs every listing known: a root listing `docs/` without a fetched `docs_dir` listing leaves it indeterminate
3. CODE_OF_CONDUCT.md (or CODE-OF-CONDUCT.md) present (root or .github/)
4. CONTRIBUTING.md present (root, .github/ or docs/, any extension) — same `docs_dir` rule as checkpoint 2
5. CODEOWNERS present (.github/, root or docs/ — where oss:gh-scraper looks, in that order); `#` comments, including trailing ones, never name an owner. Same `docs_dir` rule as checkpoint 2: a root listing `docs/` without a fetched `docs_dir` listing leaves an otherwise unlisted CODEOWNERS indeterminate
6. Branch protection enabled on default branch — the branch's `protected` flag from the branches API (readable without admin rights). The admin-only `branch_protection` rules record is optional: absent for every non-admin token (`datasets.optional_absent`), never a gap or degrader
7. Active maintainer ratio ≥0.5 — conditional: CODEOWNERS has @username entries (not @org/team) AND Axis 3 contributor stats available; cross-reference CODEOWNERS usernames against stats weeks[-13:]; active_ratio = active_90d / listed; ✓ if ≥0.5; otherwise not applicable

max_applicable = 7 if checkpoint 7 applicable, else 6 Score: floor(met / max_applicable × 10), strict (§ Unconfirmable Checkpoints); 🟢 ≥5/applicable | 🟡 3–4 | 🔴 ≤2

### Axis 8 — Security Posture

(weight reduced, partial scoring on 403 instead of excluding)

Primary signals (push access required — Dependabot alerts API):

- Open alerts by severity: critical_count, high_count, medium_count, low_count
- Secret scanning alerts

Secondary signals (always available, no push access needed):

- Dependabot/Renovate configured: `.github/dependabot.yml` or `.github/dependabot.yaml` present OR `renovate.json`/`renovate.json5`/`.renovaterc`/`.renovaterc.json`/`.renovaterc.json5` in root or `.github/`
- dep-update commits in last 90d (grep commit messages: case-insensitive `^(bump|chore\(deps\)|build\(deps\)|deps:|dependabot|update deps|upgrade deps)`)
- SECURITY.md content depth (root, .github/ or docs/): present=1pt; contains `@` email=+1pt; contains digit+("day"|"hour"|"week") SLA=+1pt; depth_score 0–3

Score when Dependabot alerts available (§ In-Band Score Placement band rule):

- high count = Dependabot high alerts + open secret-scanning alerts (each open secret-scanning alert counts as one high alert)
- 🟢: 0 critical/high alerts AND dep-config present
- 🟡: otherwise — 0 critical with 1–4 high, OR no dep-config (dep-config indeterminate does not hold)
- 🔴: ≥1 critical OR ≥5 high — beats 🟡

**B2 fix — Score when Dependabot alerts unavailable (`403` or `absent`; partial scoring; NOT excluded ⚪)**:

- partial_score = 0
- +4 if dep-config present (highest weight — proves proactive security hygiene)
- +3 if dep-update commits present (proves active dependency maintenance)
- +2 if depth_score ≥1 (SECURITY policy present — fetched, or listed with content unread)
- +1 if all three present (bonus: belt-and-suspenders)
- Label: partial_score ≥4 → 🟡, else 🔴 — never 🟢: the primary signal is missing
- Score = partial_score capped at its label's band maximum (🟡 6, 🔴 3): partial_score 7–10 scores 🟡 6. A 🟡 label never carries a 🟢-range score — uncapped, a token without alert access scored a repository 🟡 10, the same Health Score contribution as a verified 🟢 and more than an admin token's 🟡 5 for one high alert. Token access changes Axis 8 only through this 🟡 ceiling and the fixed confidence below
- Confidence = 0.4 fixed (Dependabot alerts unavailable — primary signal missing)
- Note in report: "Dependabot alerts unavailable (push access required) — score from config signals only"
- Secondary signals scored strict (§ Unconfirmable Checkpoints): unconfirmed signal adds no points; note the upper partial score

## Extractor-Computed Values

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md, plugins/cc_oss/bin/vitality_extract.py -->

**Copy rule**: `bin/vitality_extract.py` computes every axis's `band`, `score`, `conf` and `conf_degraders` from these rules; the scorer copies `band` (as `label`), `score` and `conf` verbatim — notes may explain them, never change them.

## In-Band Score Placement

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md -->

**Band rule** (band-only axes): 🔴 when any 🔴 condition holds — the worst band wins when several band lines hold; else 🟢 when every 🟢 clause holds; else 🟡. A clause on a missing (`null`) metric does not hold. Ranges are half-open: a value on a boundary two band lines share belongs to the better band — every 🟢 clause includes its boundary (`≥` / `≤`), every 🔴 condition excludes it (`>` / `<`) (close_rate 0.8, pct_responded_7d 60%, stale 10%, last commit 14d, top contributor 50% → 🟢 clause holds; stale 30%, close_rate 0.4, median issue response 21d → 🟡). Thresholds compare unrounded metrics; printed metrics are rounded for display (a close_rate of 0.795 prints 0.8 and does not hold the ≥0.8 clause).

Axis 4, and Axis 8 when Dependabot alerts are available: 🟢 10; 🔴/🟡 score = band anchor (🔴 1 · 🟡 4) + 1 per 🟢-line clause that holds, capped at band max (🔴 3 · 🟡 6). Unavailable/undefined clause does not hold. Axis 4 clauses: stale ≤10%, close_rate ≥0.8, merge_rate ≥0.7, review_coverage ≥80%. Axis 8 clauses: 0 critical/high alerts, dep-config present.

**Confidence — listed degraders only** (Weights table in `vitality-scoring.md`):

**Confidence formula**: conf = 1.0 − each listed degrader whose condition the data shows − 0.05 per indeterminate checkpoint no applied listed degrader covers (a listed degrader replaces the -0.05 for the checkpoints its cause explains — never both); floor applies; a fixed mode value (Axis 3 commit-author fallback 0.5, Axis 8 Dependabot alerts unavailable 0.4) replaces the formula.

Unlisted concern (small sample, odd value, extractor limit, unlisted truncation) → `notes`, never `conf`. `conf_degraders` names each applied degrader + delta.

## Unconfirmable Checkpoints

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/agents/repo-warden.md -->

Checkpoint states come from `vitality_extract.py`: `met` / `unmet` / `indeterminate` (input not fetched or unreadable) / `not_applicable`.

- Score strict — indeterminate counts as unmet; band from the met count.
- Never credit from a name, a midpoint, or a guess about unread content.
- Notes state the upper bound `floor((met + indeterminate) / applicable × 10)` when any checkpoint is indeterminate.
- Confidence per the Confidence formula: a listed degrader for the cause replaces the -0.05 for the checkpoints it explains — never both.

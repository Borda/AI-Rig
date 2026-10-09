<!-- file: vitality-scoring-group-c.md — consumers: oss/agents/repo-warden.md -->

# Vitality Scoring Rubrics — Group C

> Axes 3, 9 — scored by oss:repo-warden AXIS_GROUP=C (Axis 3 scored first, feeds Axis 9A)
>
> Split from `vitality-scoring.md` (see that file for Weights & Confidence Thresholds table, Advisory Signals, Implementation Status).

## Axes

### Axis 3 — Contributor Health

(individual concentration + community sustainability)

- Filter bot accounts from contributor list. **Bot rule** (extractor `is_bot` / `_is_bot_actor`, used by every bot filter in this file): GitHub's `is_bot` flag or a GraphQL `__typename` of `Bot` when the data carries one; otherwise a login ending in `[bot]` or `-bot`, starting with `app/` (how `gh` renders GitHub App authors), or one of the known automation names `pre-commit-ci`, `mergify`, `allcontributors`, `renovate`, `dependabot`, `codecov`, `copilot-pull-request-reviewer`, `socket-security`, `claassistant`. Unknown names count as human (under-filter rather than drop a contributor)
- Bus factor = min contributors whose removal drops 90d commit total >50% (sort desc by 90d commits; accumulate until >50%)
- Top contributor % of total commits last 90d
- **Contributor retention rate**: using stats weeks[-13:] (last 90d):
  - Q1 = weeks[-13:-7] (90d–45d ago); Q2 = weeks[-7:] (45d–0d ago)
  - active_Q1 = contributors with sum(Q1.c) ≥1
  - active_both = contributors active in Q1 AND Q2
  - retention_rate = len(active_both) / len(active_Q1) — undefined if len(active_Q1) = 0

Score (band rule below — worst band wins):

- 🟢: bus factor ≥3 AND top contributor ≤50% last 90d AND retention ≥50%
- 🟡: otherwise — e.g. bus factor 2, top contributor >50% to ≤75%, retention 30% to \<50%, or retention undefined (undefined never holds the 🟢 clause)
- 🔴: (bus factor = 1 AND Axis 2 🔴) OR top contributor >75% OR retention \<30% OR no non-bot commit in the last 90 days (`all_90d_weeks_zero` — nobody is contributing; decided data, never the 🟡 an all-undefined clause set would give) — beats 🟡. `axis2_band` comes from the extractor, which reads the Axis 2 inputs for Group C too

**202 fallback** (H4 fix — do not always mark ⚪) <!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md (canonical), plugins/cc_oss/agents/repo-warden.md, plugins/cc_oss/bin/vitality_extract.py -->:

- Stats `202_pending` or `absent`: approximate bus factor (`fallback.approx_bus_factor`) = distinct non-bot authors in `commits_50` (the last 50 commits; the unresolved `unknown` author is skipped), capped at 3
- Top contributor and retention unavailable → 🟢 unreachable; 🔴 only via bus factor 1 AND Axis 2 🔴
- Confidence fixed 0.5; add "⚠ bus factor estimated from commit authors (stats API computing)"
- Mark ⚪ only if stats are unavailable AND `commits_50` has no non-bot author

Stats unavailable WITH a fallback author: ⚪ NOT used; fallback score at confidence 0.5.

## Extractor-Computed Values

<!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md, plugins/cc_oss/bin/vitality_extract.py -->

**Copy rule**: `bin/vitality_extract.py` computes every axis's `band`, `score`, `conf` and `conf_degraders` from these rules; the scorer copies `band` (as `label`), `score` and `conf` verbatim — notes may explain them, never change them.

**In-band score placement** — <!-- policy-sibling: plugins/cc_oss/skills/_shared/vitality-scoring.md (canonical), plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md, plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md, plugins/cc_oss/agents/repo-warden.md -->

**Band rule** (band-only axes): 🔴 when any 🔴 condition holds — the worst band wins when several band lines hold; else 🟢 when every 🟢 clause holds; else 🟡. A clause on a missing (`null`) metric does not hold. Ranges are half-open: a value on a boundary two band lines share belongs to the better band — every 🟢 clause includes its boundary (`≥` / `≤`), every 🔴 condition excludes it (`>` / `<`) (close_rate 0.8, pct_responded_7d 60%, stale 10%, last commit 14d, top contributor 50% → 🟢 clause holds; stale 30%, close_rate 0.4, median issue response 21d → 🟡). Thresholds compare unrounded metrics; printed metrics are rounded for display (a close_rate of 0.795 prints 0.8 and does not hold the ≥0.8 clause).

Axis 3: 🟢 10; 🔴/🟡 score = band anchor (🔴 1 · 🟡 4) + 1 per 🟢-line clause that holds, capped at band max (🔴 3 · 🟡 6). Clauses: bus factor ≥3, top contributor ≤50%, retention ≥50%. Unavailable/undefined clause does not hold; fallback bus factor counts as its approximated value. Axis 9: 🟢 scores its sub-signal mean; 🟡/🔴 cap that mean at band max (🟡 6 · 🔴 3).

**Confidence — listed degraders only** (Axes 3, 9 — Weights table in `vitality-scoring.md` plus the Axis 9 confidence block):

**Confidence formula**: conf = 1.0 − each listed degrader whose condition the data shows − 0.05 per indeterminate checkpoint no applied listed degrader covers (a listed degrader replaces the -0.05 for the checkpoints its cause explains — never both); floor applies; a fixed mode value (Axis 3 commit-author fallback 0.5, Axis 8 Dependabot alerts unavailable 0.4) replaces the formula.

Unlisted concern (small sample, odd value, extractor limit, unlisted truncation) → `notes`, never `conf`. `conf_degraders` names each applied degrader + delta.

### Axis 9 — Trajectory

(momentum direction: accelerating or decelerating?)

Four sub-signals, each scored 0–10; overall axis score = mean of available sub-signals, capped at the band maximum in 🟡/🔴. Requires: merged PRs last 90d (Group 1 new fetch), last 50 commits (Group 1 new fetch), open issues (reused from Axis 4), contributor stats weeks[] (reused from Axis 3).

**Sub-signal 9A — Reviewer pool drift** (uses Axis 3 contributor stats weeks[])

Computation:

- If stats 202 after all retries AND fallback used: pool_drift = undefined (sub-signal 9A unavailable)
- window_recent = weeks[-26:] (last ~6 months); window_prior = weeks[-52:-26] (months 7–12)
- Filter bots: exclude bot accounts (Axis 3 bot rule)
- pool_recent = set of logins with sum(window_recent.c) >= 1
- pool_prior = set of logins with sum(window_prior.c) >= 1
- If len(pool_prior) == 0: sub-signal 9A = ⚪ (no baseline — repo too young or stats sparse)
- shrinkage_ratio = (len(pool_prior) - len(pool_recent)) / len(pool_prior)
  - positive = shrinking; negative = growing
- departed = pool_prior - pool_recent; arrived = pool_recent - pool_prior

Score 9A:

- 🟢 (10): shrinkage_ratio ≤ 0 (pool stable or growing)
- 🟡 (5): 0 < shrinkage_ratio ≤ 0.30 (up to 30% shrinkage)
- 🔴 (0): shrinkage_ratio > 0.30 OR len(pool_recent) == 0 (zero active mergers last 6m)

**Sub-signal 9B — Time-to-merge trend** (uses merged PRs last 90d fetch)

Computation:

- Filter bot PRs (author is a bot account — Axis 3 bot rule)
- Per merged PR: merge_days = (mergedAt - createdAt) in fractional days
- window_30d = PRs where mergedAt >= CUTOFF_30D (last 30 days)
- window_90d = all PRs in 90d fetch (full 90-day window)
- median_30d = median(merge_days for PRs in window_30d)
- median_90d = median(merge_days for all PRs in window_90d)
- `merged_prs_90d` missing → sub-signal 9B = ⚪ (never 🔴 from absent data)
- If len(window_30d) == 0: signal = "no_merges_30d" → 🔴
- If len(window_90d) < 5: trend unstable (confidence degrader -0.2 applies); compute anyway
- trend_ratio = median_30d / median_90d from unrounded medians (> 1 = worsening; < 1 = improving); median_90d = 0 → 1.0 when median_30d = 0 too, else no finite ratio → 🔴

Score 9B:

- 🟢 (10): len(window_30d) >= 1 AND trend_ratio \<= 1.0 (improving or stable)
- 🟡 (5): 1.0 < trend_ratio \<= 2.0 (up to 2× worse)
- 🔴 (0): trend_ratio > 2.0 OR len(window_30d) == 0 (no merges last 30d)

**Sub-signal 9C — Queue staleness depth** (uses open issues list from Axis 4)

Computation:

- Use open issues list already fetched (--limit 501 with truncation detection)
- Per open issue: age_days = (ANALYSIS_NOW - createdAt) / 86400
- Sort ages ascending; P90 = value at 90th percentile position
  - p90_index = int(len(ages) * 0.90); p90_age_days = sorted_ages[p90_index]
- If len(open_issues) == 0: sub-signal 9C = ⚪ (no open issues — repo uses discussions or closed everything)
- If open issue list truncated (501 returned): note "P90 computed over 500-issue sample — actual P90 may be higher"; confidence -0.1

Score 9C:

- 🟢 (10): p90_age_days ≤ 30
- 🟡 (5): 30 < p90_age_days ≤ 180
- 🔴 (0): p90_age_days > 180

**Sub-signal 9D — Commit automation ratio** (uses last 50 commits fetch)

Rationale: dep-bump merges by human maintainers = legitimate maintenance work; only fully bot-authored commits indicate zero human engagement

Computation:

- automated_count = commits where BOTH conditions hold: (a) message matches dep-bump pattern (case-insensitive, anchored at start): `^(bump|chore\(deps\)|build\(deps\)|dependabot|renovate|update deps|upgrade deps)` AND (b) author is a bot account (Axis 3 bot rule)
- total_count = len(commits fetched)
- auto_ratio = automated_count / total_count
- If total_count < 10: sub-signal 9D confidence degraded -0.1; compute anyway
- If total_count == 0: sub-signal 9D = ⚪ (no commits — unlikely but guarded)

Score 9D:

- 🟢 (10): auto_ratio ≤ 0.50 (at most half of recent commits are bot dependency bumps)
- 🟡 (5): 0.50 < auto_ratio ≤ 0.90 (automated majority but human commits present)
- 🔴 (0): auto_ratio > 0.90 (nearly all commits bot-authored — possible zombie-maintenance or fork-only repo)

**Axis 9 overall score:**

- available_subs = sub-signals not marked ⚪
- If len(available_subs) == 0: Axis 9 = ⚪ (all sub-signals unavailable)
- AXIS9_MEAN = mean(score for sub in available_subs) — 0–10 float
- Status: 🟢 if AXIS9_MEAN >= 7.5 | 🟡 if AXIS9_MEAN >= 3.75 | 🔴 if AXIS9_MEAN < 3.75
- Score: 🟢 = AXIS9_MEAN; 🟡 = min(AXIS9_MEAN, 6); 🔴 = min(AXIS9_MEAN, 3) — the band maximum every axis respects (10/10/5/0 → mean 6.25 → 🟡 6; 10/0/0 → 3.33 → 🔴 3)

**Axis 9 confidence:**

- Base: 1.0
- -0.2 if len(window_90d) < 5 (time-to-merge trend unstable — too few merged PRs in window)
- -0.2 if contributor stats unavailable (`stats_status` `202_pending` or `absent` — reviewer pool unknown, sub-signal 9A unavailable)
- -0.1 if total commit count < 10 (substance ratio from sparse sample)
- -0.1 if open issue list truncated (P90 computed over partial set)
- -0.1 if merged PR list truncated AND `window_days_covered` \<90 (9B 90d median from a partial window; an untruncated list covering fewer days is complete)
- Floor: 0.3

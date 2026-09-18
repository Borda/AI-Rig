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
- close rate = closed last 30d / opened last 30d (0 if denominator 0); if stale-bot config present (`.github/stale.yml` or `stale` in workflow names) AND close_rate ≥2.0, flag "⚠ potential stalebot inflation" in report — stalebot auto-closes inflate close_rate without resolution
- median open issue age (days)

PR signals (from open/closed PR lists; filter bot PRs):

- merge rate = merged last 30d / opened last 30d (bot-filtered)
- abandoned % = open PRs with no update >30d / total open
- closed-without-merge ratio = closed PRs with mergedAt=null / total closed last 30d

Code-review coverage (from GraphQL, last 30 merged PRs; filter bot PRs):

- `review_coverage` = count(PRs with ≥1 non-author approving review) / count(all non-bot merged PRs sampled)
- "undefined" if \<5 non-bot merged PRs in sample

Score (worst-of composite — any 🔴 dimension → axis 🔴):

- 🟢: stale \<10% AND close_rate ≥0.8 AND merge_rate ≥0.7 AND review_coverage ≥80%
- 🟡: stale 10–30% OR close_rate 0.4–0.8 OR merge_rate 0.3–0.7 OR review_coverage 50–80% OR review_coverage undefined
- 🔴: stale >30% OR close_rate \<0.4 OR merge_rate \<0.3 OR review_coverage \<50%

### Axis 7 — Governance

(7 checkpoints, weight increased above Documentation per H1 fix)

1. LICENSE present (root)
2. SECURITY.md present (root or .github/)
3. CODE_OF_CONDUCT.md present (root or .github/)
4. CONTRIBUTING.md present (root or .github/)
5. CODEOWNERS present (.github/ or root)
6. Branch protection enabled on default branch
7. Active maintainer ratio ≥0.5 — conditional: CODEOWNERS has @username entries (not @org/team) AND Axis 3 contributor stats available; cross-reference CODEOWNERS usernames against stats weeks[-13:]; active_ratio = active_90d / listed; ✓ if ≥0.5

max_applicable = 7 if checkpoint 7 applicable, else 6 Score: floor(met / max_applicable × 10); 🟢 ≥5/applicable | 🟡 3–4 | 🔴 ≤2

### Axis 8 — Security Posture

(weight reduced, partial scoring on 403 instead of excluding)

Primary signals (push access required — Dependabot alerts API):

- Open alerts by severity: critical_count, high_count, medium_count, low_count
- Secret scanning alerts

Secondary signals (always available, no push access needed):

- Dependabot/Renovate configured: `.github/dependabot.yml` present OR `renovate.json`/`.renovaterc` in root-contents
- dep-update commits in last 90d (grep commit messages: case-insensitive `^(bump|chore\(deps\)|build\(deps\)|deps:|dependabot|update deps|upgrade deps)`)
- SECURITY.md content depth: present=1pt; contains `@` email=+1pt; contains digit+("day"|"hour"|"week") SLA=+1pt; depth_score 0–3

Score when Dependabot available (no 403):

- 🟢: 0 critical/high alerts AND dep-config present
- 🟡: 0 critical but ≥1 high OR no dep-config
- 🔴: ≥1 critical OR ≥5 high

**B2 fix — Score when Dependabot 403 (partial scoring; NOT excluded ⚪)**:

- partial_score = 0
- +4 if dep-config present (highest weight — proves proactive security hygiene)
- +3 if dep-update commits present (proves active dependency maintenance)
- +2 if depth_score ≥1 (SECURITY.md with contact or SLA)
- +1 if all three present (bonus: belt-and-suspenders)
- Score = min(10, partial_score)
- Confidence = 0.4 (Dependabot alerts unavailable — primary signal missing)
- Note in report: "Dependabot alerts unavailable (push access required) — score from config signals only"
- Never 🟢 when Dependabot 403; max effective 🟡 from partial scoring when all secondary signals present

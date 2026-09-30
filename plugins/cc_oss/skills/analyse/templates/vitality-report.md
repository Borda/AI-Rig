<!-- vitality-report.md — output template for oss:analyse vitality mode
     Read this file in Step 4 to structure the $REPORT_FILE Write call.
     Named {VARIABLE} placeholders are substituted from values set in Steps 1–4.
     Do not modify section order — downstream tools (Step 5 aggregation, Step 6 rework) locate sections by heading. -->

> - Replace square-bracket placeholders with this run's findings and values; omit conditional sections and bullets when inapplicable.
> - Front matter: list REPORT_AGENTS_YAML agents as a comma-joined line in the body.
> - Summary: write 2–3 sentences covering overall health, top strength, top risk, health score badge, and axis tally.
> - Findings: use fetched data as evidence, explain project impact, and give concrete actions. Critical/high entries include score and confidence.
> - Duplicate groups: identify the shared root; mark canonical and duplicate items; label related items with distinct problems as RELATED; recommend closing duplicates against the canonical item.
> - Data Sources: counts are actual response sizes, not estimates. Open issues truncate at 500; closed issues truncate at 1,000. Note truncation and report any 403 or 202 endpoint in Gaps & Limitations.
> - Overall confidence below 0.7 requires: "⚠ Health Score reliability is LOW — findings are directional only. Re-run with improved data access before acting on this score." List only axes below 1.0 in the per-axis table; when all are at least 0.9, state "All axes scored with high-confidence data."
> - Structural signals and per-run recommendations: include only applicable items. For the low-volume Axis 4 condition, set the recheck date 30 days ahead.
> - Independent Codex Review: include Step 5's read-only bridge assessment and aggregation; when unavailable, state "bridge unavailable — single-pass analysis only."
> - Adversarial Review: always include challenger findings. Include bridge findings when bridge@borda-ai-rig is installed and enabled; otherwise state "bridge unavailable — single adversarial pass only". Add " and bridge:review" to the sign-off line when CODEX_AVAILABLE=1.

```markdown
---
generated_at: {REPORT_TIMESTAMP}
repo: {GH_OWNER}/{GH_REPO}
skill: oss:analyse
mode: vitality
skill_version: {SKILL_VERSION}
commit: {REPORT_COMMIT}
passes: {TOTAL_PASSES}
confidence_history: [{CONFIDENCE_HISTORY with colons replaced by ", "}]
agents:
{REPORT_AGENTS_YAML}
---

# Repo Vitality — {GH_OWNER}/{GH_REPO}

**Generated:** {REPORT_TIMESTAMP}
**Skill:** oss:analyse · mode: vitality · v{SKILL_VERSION}
**Commit:** {REPORT_COMMIT}
**Agents:** [agent list]

---

## Summary

[summary]

**Health Score:** [score]% · [status: 🟢/🟡/🔴] · [N] healthy · [N] warning · [N] critical · [N] unavailable (⚪)

---

## Scorecard

| Axis | Weight | Score | Status | Conf | Key Signal |
|------|--------|-------|--------|------|------------|
| 1 Responsiveness | {WEIGHT_1}% | [score] | [🟢/🟡/🔴] | [confidence] | median issue Xd, PR Xd; X% ≤7d |
| 2 Maintenance activity | {WEIGHT_2}% | [score] | [🟢/🟡/🔴] | [confidence] | last commit Xd, X commits/30d |
| 3 Contributor health | {WEIGHT_3}% | [score] | [🟢/🟡/🔴] | [confidence] | bus factor N, retention X% |
| 4 Issue & PR health | {WEIGHT_4}% | [score] | [🟢/🟡/🔴] | [confidence] | stale X%, close rate X, review cov X% |
| 5 CI/CD & code quality | {WEIGHT_5}% | [score] | [🟢/🟡/🔴] | [confidence] | N/5 checks, CI pass rate X% |
| 6 Documentation | {WEIGHT_6}% | [score] | [🟢/🟡/🔴] | [confidence] | N/9 checkpoints |
| 7 Governance | {WEIGHT_7}% | [score] | [🟢/🟡/🔴] | [confidence] | N/7 files, active maintainers X/Y |
| 8 Security posture | {WEIGHT_8}% | [score] | [🟢/🟡/🔴] | [confidence] | dep-config: yes/no, alerts: N or "403" |
| 9 Trajectory | {WEIGHT_9}% | [score] | [🟢/🟡/🔴] | [confidence] | pool drift: ±N%, TTM 30d: Xd vs 90d: Yd, P90 queue: Zd, dep-bump: X% |
| **Total Score** | 71%* | **[score]%** | [🟢/🟡/🔴] | — | — |

_(Conf: per-axis confidence 0.00–1.00; ⚠ = below 0.9. *Axes 1–9 weight 71% of the full 13-axis rubric; axes 10–13 (29%) not yet implemented. ⚪ axes excluded from score; weight renormalized over available axes.)_

---

## Finding Matrix

Quick severity cross-reference across all axes:

| Axis | Critical | High | Medium | Low | Total |
|------|----------|------|--------|-----|-------|
| 1 Responsiveness | N | N | N | N | N |
| 2 Maintenance activity | N | N | N | N | N |
| 3 Contributor health | N | N | N | N | N |
| 4 Issue & PR health | N | N | N | N | N |
| 5 CI/CD & code quality | N | N | N | N | N |
| 6 Documentation | N | N | N | N | N |
| 7 Governance | N | N | N | N | N |
| 8 Security posture | N | N | N | N | N |
| 9 Trajectory | N | N | N | N | N |
| **Total** | **N** | **N** | **N** | **N** | **N** |

_Severity mapping: 🔴 axis score = Critical or High findings; 🟡 = Medium; Low = informational observations within any axis._

---

## Findings

Ordered by severity. Each finding includes evidence from fetched data, impact assessment, and concrete action.

### 🔴 Critical

_(Only emitted when axis scores 🔴 with high-impact evidence. If none: "No critical findings.")_

#### [Axis N] [finding title]
**Axis:** [axis name] · **Score:** [score] · **Conf:** [confidence]
**Evidence:** [evidence]
**Impact:** [impact]
**Action:** [action]

### 🟡 High

_(🟡 axes with strong signal. If none: "No high-severity findings.")_

#### [Axis N] [finding title]
**Axis:** [axis name] · **Score:** [score] · **Conf:** [confidence]
**Evidence:** [evidence]
**Impact:** [impact]
**Action:** [action]

### 🟠 Medium

_(Informational issues within 🟡 axes, or boundary-case 🔴 axes with mitigating factors.)_

### 🔵 Low

_(Observations worth tracking but not urgent.)_

---

## Duplicate Clustering

Group all issues, PRs, and discussions (open and closed) by shared duplication root —
specific element that makes them same problem: identical error message, identical
feature ask, or identical root cause even if symptoms differ. Flag as RELATED (not duplicate)
when items share component/area but have distinct problems.

#### Group 1
**Root**: [shared root]
- Issue #N: [title] ([open/closed]) — created [date]  ← CANONICAL
- Issue #N: [title] ([open/closed]) ← DUPLICATE
- PR #N: [title] ([state]) ← related fix
  → Close duplicates with: "Closing as duplicate of #[canonical]"

_(Repeat for each group. If no duplicate groups found: "No obvious duplicates detected.")_

### Open-PR Overlap

Merge-conflict / duplicate-effort candidates among open PRs (Signal B). Direct = shared changed files; Structural = tightly-coupled modules touched by different PRs (codemap-py). Emit only when candidates found:

- **PRs #A and #B** — direct: both touch `path/to/file` → conflict/duplicate candidate.
- **PRs #A and #C** — structural: touch coupled modules `m1`/`m2` (no shared files) → review together.

_(No candidates: "No overlapping open PRs detected." When skipped: "PR-set overlap skipped — {N} open PRs exceeds cap {PR_FILES_CAP}.")_

---

## Recommended Actions

Ordered by priority (highest impact first):

1. [action] — addresses [axis], expected impact: [outcome]
2. ...

---

## Independent Codex Review

[Codex review or availability status]

---

## Data Sources

All data fetched from GitHub API at {REPORT_TIMESTAMP}. Record counts confirm analysis is grounded in actual repository state.

| Source | API Endpoint | Records Fetched | Window | Notes |
|--------|-------------|-----------------|--------|-------|
| Open issues | `GET /repos/{repo}/issues?state=open` | N | all open | [truncation status] |
| Closed issues | `GET /repos/{repo}/issues?state=closed` | N | last 3 years | time-bounded; [truncation status] |
| Open PRs | `GET /repos/{repo}/pulls?state=open` | N | all open | |
| Closed PRs | `GET /repos/{repo}/pulls?state=closed` | N | recent 200 | merge rate window |
| Commits | `GET /repos/{repo}/commits` | N | last 100 | date range: [earliest]–[latest] |
| Releases | `GET /repos/{repo}/releases` | N | last 10 | cadence + downloads |
| Contributor stats | `GET /repos/{repo}/stats/contributors` | N contributors | all-time | [202 fallback status] |
| Responsiveness sample | GraphQL issues + PRs | 20 + 20 | most recent | time-to-first-response |
| CI workflows | `GET /repos/{repo}/actions/workflows` | N workflows | — | |
| CI runs | `GET /repos/{repo}/actions/runs` | N | last 20 | pass rate |
| README | `GET /repos/{repo}/readme` | [size] bytes | — | |
| Dependabot alerts | `GET /repos/{repo}/dependabot/alerts` | [N or 403] | open | 403 = no push access |
| Star history | `GET /repos/{repo}/stargazers` | N | last 180d | advisory only |
| Merged PRs 90d | `GET /repos/{repo}/pulls` (closed, merged:≥90d) | N | last 90d | Axis 9 TTM trend + reviewer pool |
| Commit messages | `GET /repos/{repo}/commits?per_page=50` | N | last 50 | Axis 9 substance ratio |

_[Data completeness note]_

---

## Methodology

Axes and weights reflect signal quality, data reliability, predictive value for project sustainability. Sources: CHAOSS practitioner guides, OpenSSF Scorecard risk levels, repohealth category weights.

| Axis | Weight | Rationale |
|------|--------|-----------|
| 1 Responsiveness | 10% | CHAOSS top metric — time-to-first-response is the most visible signal to contributors and directly predicts contributor retention |
| 2 Maintenance activity | 8% | Commit velocity is objective evidence the project is alive; cannot be gamed by documentation alone |
| 3 Contributor health | 10% | Bus factor and retention rate predict abandonment risk 1–2 quarters before commit counts drop |
| 4 Issue & PR health | 7% | Throughput + code-review coverage; merged from two prior axes to avoid double-counting maintainer behaviour |
| 5 CI/CD & code quality | 7% | Projects with CI accumulate fewer silent regressions; absence correlates with abandonment (repohealth: CI/CD = 35/100) |
| 6 Documentation | 5% | Lagging usability signal — content depth weighted over presence; lower than governance because docs are easier to retrofit |
| 7 Governance | 6% | Legal usability (LICENSE), security contact (SECURITY.md), succession planning (CODEOWNERS) — harder to retrofit than docs |
| 8 Security posture | 11% | Highest of axes 1–9 — security incidents carry outsized project risk even though the primary signal (Dependabot alerts) requires push access; most runs score via secondary signals at confidence 0.4 |
| 9 Trajectory | 7% | Momentum direction — reviewer pool drift, time-to-merge trend, queue staleness P90, and commit substance ratio together detect deceleration 1–2 quarters before Axis 2 (activity) flatlines |

Axes 1–9 weights sum to 0.71 (71% of the full 13-axis rubric); axes 10–13 (29%) are not yet implemented. Axes where data is unavailable (⚪) are excluded; remaining weights renormalized over the available axes.

---

## Gaps & Limitations

**Overall confidence:** {overall_confidence:.2f} [🟢 ≥0.9 / 🟡 0.7–0.9 / 🔴 <0.7]

[low-confidence warning, if applicable]

### Per-Axis Confidence

| Axis | Confidence | Gap | Score Impact |
|------|------------|-----|--------------|
| [axes below 1.0 confidence, sorted ascending] | | | |

_(Only axes with confidence < 1.0 appear. If all ≥ 0.9: "All axes scored with high-confidence data.")_

### Structural Constraints

Permanent limitations — will not resolve by re-running. Emit only when applicable:

- **Axis 7 branch protection**: requires admin/push scope — endpoint returns 404 without elevated access; cannot confirm whether direct pushes to default branch are blocked. Grant admin scope or check branch protection manually in GitHub Settings.
- **Adversarial review skipped**: Agent tool unavailable — skill was invoked without Agent capability (e.g. direct Bash execution, restricted context). Adversarial review is mandatory; re-run via `/oss:analyse vitality` in a full Claude Code session.
- **Axis 5 SAST — inference only**: no SAST workflow file detected; SAST signal inferred from config files or naming patterns — one additional inference step reduces confidence. Add a dedicated SAST workflow to resolve.

**Structural (codemap-py)** — populated from `central --top 5` + index coverage when codemap-py available; single "unavailable" bullet otherwise:

- **Highest blast radius**: `[module]` ([N] reverse-deps) — changes here ripple widest; weight review effort accordingly. _(top 1–5 modules)_
- **Symbol collisions**: [N] name collisions in the index — rename/find-symbol precision reduced for those names. _(omit when 0)_
- **Index degraded**: built in degraded mode — some structural signals approximate. _(omit unless degraded)_
- **Index stale**: lags recent commits — structural figures may miss latest changes. _(omit unless stale)_
- **Structural index unavailable**: blast-radius / collision signals not computed — codemap plugin absent or no index (build via `/codemap-py:scan-codebase`, requires codemap plugin). _(this bullet only, when codemap disabled)_

_(Omit bullets that do not apply to this run.)_

### Per-Run Recommendations

Limitations that may resolve on re-run:

- **Axis 8 full data**: re-run with push access — Dependabot alert counts then available
- **Axis 3**: re-run in 5–10 min — contributor stats computing (202); retry when complete
- **Axis 4 merge rate**: re-run after [recheck date] — low-volume repo; <3 PRs this month makes rate unstable

_(Omit bullets that do not apply to this run.)_

---

## Adversarial Review

**Challenger:** [challenger findings]

**Codex:** [Codex findings/status]

---

## Sign-off & Disclaimer

Report generated by **oss:analyse v{SKILL_VERSION}** on commit `{REPORT_COMMIT}` at `{REPORT_TIMESTAMP}`.
Data sourced exclusively from GitHub API — no manual input or cached external data.
Scores reflect repository state at time of generation. Re-run for current state.
Adversarial review performed by foundry:challenger[bridge attribution].
```

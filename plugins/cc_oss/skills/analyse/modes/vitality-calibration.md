Validate scoring range + sensitivity across known archetypes. Run `/oss:analyse vitality` on each repo below; compare output Health Score % against expected range

## Scenario Matrix

| Archetype | Scale | Expected Health Score | Key discriminator |
| -- | -- | -- | -- |
| Active, well-governed | Large (>50k stars) | 80–95% | Axes 1+2+7 all 🟢 |
| Active, well-governed | Mid (5k–50k stars) | 68–85% | Axes 1+2+4 all 🟢 |
| Active, solo maintainer | Small (\<5k stars) | 42–65% | Axis 3 🔴 drags score |
| Archived / abandoned | Mid | 12–28% | Axes 1+2+4 all 🔴 |
| Never-governed, dead | Small | 2–12% | Axes 1+2+3+7 all 🔴 |
| Accelerating (new contributors, shrinking TTM, low dep-bumps) | Any | — | Axis 9 🟢 (≥7.5) |
| Decelerating (reviewer pool shrinking, queue growing, dep-bump-only output) | Any | — | Axis 9 🔴 (\<3.75; 3.75 itself is 🟡) |

## Concrete Test Repos

**Active & healthy — expect high scores:**

- `pytest-dev/pytest-mock` — small, active, good governance → expect **76–84%**
- `pallets/click` — mid, active, excellent docs+governance → expect **80–88%**
- `django/django` — large, extremely active, top-tier governance → expect **85–95%**

**Axis 9 trajectory signals for active repos:**

- `pytest-dev/pytest-mock` — small active: pool stable (🟢), TTM improving (🟢), low dep-bumps (🟢) → Axis 9 🟢
- `django/django` — large active: pool stable or growing (🟢), rapid TTM (🟢), very low dep-bump ratio (🟢) → Axis 9 🟢

**Abandoned / archived — expect low scores:**

- `jpadilla/django-rest-framework-jwt` — archived May 2020, mid (3.2k stars) → expect **12–22%**
- `mozilla/bleach` — deprecated 2023-01-23 (self-declared; NOT GitHub-archived — still read-write), mid (2.8k stars), good historical governance → expect **26–40%**
- `jakevdp/JSAnimation` — abandoned ~2017 (functionality merged into matplotlib 2.1), small (240 stars), minimal governance → expect **2–12%**

**Axis 9 trajectory signals for abandoned repos:**

- `jpadilla/django-rest-framework-jwt` — archived: pool_recent = 0 (🔴), no merges last 30d (🔴), P90 queue age >> 180d (🔴) → Axis 9 🔴
- `mozilla/bleach` — deprecated: similar pattern; P90 queue age > 180d; dep-bump ratio varies → Axis 9 🔴

## Per-Repo Expected Axis Status

| Repo | A1 Resp | A2 Maint | A3 Contrib | A4 Issue/PR | A5 CI/CD | A6 Docs | A7 Gov | A8 Sec | A9 Traj |
| -- | -- | -- | -- | -- | -- | -- | -- | -- | -- |
| pytest-mock | 🟢 | 🟢 | 🟡 | 🟢/🟡\* | 🟢 | 🟢 | 🟢 | 🟡† | 🟢 |
| click | 🟢 | 🟢 | 🟡 | 🟢 | 🟢 | 🟢 | 🟢 | 🟡† | 🟢 |
| django | 🟢 | 🟢 | 🟢/⚪‡ | 🟢 | 🟢 | 🟢 | 🟢 | 🟡† | 🟢 |
| jwt | 🔴 | 🔴 | 🟡 | 🔴 | 🔴 | 🟡 | 🟡 | 🟡† | 🔴§ |
| bleach | 🔴 | 🔴 | 🟡 | 🔴 | 🟡 | 🟢 | 🟢 | 🟡† | 🔴§ |
| JSAnimation | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴 | 🔴† | 🔴§ |

- `*` review coverage volatile when \<5 merged PRs in window
- `†` Dependabot 403 in calibration runs (no push access) — partial scoring from secondary signals; confidence 0.4
- `‡` large repo — stats 202 likely; fallback to commit-author bus factor (confidence 0.5)
- `§` abandoned repos: pool_recent = 0 or near-zero → 9A 🔴; no merges last 30d → 9B 🔴; P90 queue age >> 180d → 9C 🔴

## Pass Criteria

- Active repos: Health Score ≥65%
- Abandoned repos: Health Score ≤40%
- **Axis 2 must be 🔴 for all abandoned repos** — hard requirement; any 🟢/🟡 on jwt/bleach/JSAnimation = B1 scoring bug (commits/30d = 0 must always → 🔴 regardless of release)
- **Axis 1 must be 🔴 for all abandoned repos** — no maintainer responses expected; pct_unresponded should be >60%
- **Axis 8 never 🟢 in calibration runs** — Dependabot 403 always expected; 🟢 = bug
- Size neutrality: score must not bias by repo size — run small vs large active repos; diff must be ≤15% for same governance quality
- Axis 3 fallback: when contributor stats 202 → report must show "⚠ bus factor estimated from commit authors" footnote; verify for django
- Determinism: re-run same repo twice same UTC day → Health Score % identical (0% drift); any diff = reproducibility bug
- Coverage gate: run scenario where ≥4 axes return 403/202 → verify score shows `⚠ provisional` flag
- Review coverage: test solo-maintainer repo with known self-merge pattern → review_coverage \<50% → Axis 4 🔴
- pytest-mock Axis 4 caveat: review coverage volatile with \<5 merged PRs — validate across ≥3 months before treating definitive
- **Cross-run consistency**: when two repos analysed in same session, CI pass-rate and security severity must use identical denominators and classification rules; any asymmetry = B4/B2 bug respectively
- **Silence rate primary**: Axis 1 scorecard row must show silence rate (% unresponded) as first metric; TTFR as secondary; absence of silence rate in row = formatting bug
- **Axis 9 must be 🔴 for all abandoned repos** — pool_recent = 0, TTM no merges last 30d, P90 queue age > 180d together guarantee 🔴; any 🟢/🟡 = scoring bug
- **Axis 9 sub-signal independence**: 🔴 on sub-signal 9B (no merges 30d) alone pulls axis score below 7.5 even when other sub-signals 🟢 — verify composite behavior

## Failure Modes to Watch

- **B1 false positive**: repo with old release but zero commits in 90d → Axis 2 must score 🔴; any 🟡 = bug
- **Responsiveness with no issues**: new repo with 0 open issues — Axis 1 confidence degraded; score from PR response only; note "Axis 1 low sample — \<5 issues sampled" in Gaps
- **Bot-only PRs**: all recent merged PRs are Dependabot/Renovate → review coverage undefined (all filtered); note "review coverage: bot PRs excluded, insufficient human PR sample" in Key Signal
- **Discussions-only support**: maintainer routes support to Discussions → Axis 4 issue close rate may appear artificially low; note discussion volume in Key Signal when discussions count > open issues count
- **No CI at all** (legacy or non-code repos): Axis 5 🔴 expected for all abandoned repos and some small projects; not a bug
- bleach: deprecated but NOT GitHub-archived — API endpoints still active; Axis 2 must still be 🔴 (no new commits since deprecation); Axes 6 and 7 may be 🟢 (governance files intact from pre-deprecation era — correct, not a bug)
- JSAnimation: 240 stars — Axis 1 confidence likely low (\<5 issues); score may be ⚪ or from PR sample only; acceptable
- Large repos: contributor stats 202 → Axis 3 fallback to commit-author bus factor (confidence 0.5); terminal must show ⚠ footnote
- Repos with no releases: Axis 2 release cadence unavailable; must not penalise — cadence is optional sub-signal
- Monorepos: root-contents check may miss docs/ inside subdirs — known limitation; note if detected
- Axis 7 checkpoint 7 requires both CODEOWNERS and Axis 3 stats; repos with only @org/team entries in CODEOWNERS (no individual @username) skip checkpoint 7 — expected behaviour
- **Axis 9 stats dependency**: sub-signal 9A (reviewer pool drift) depends on Axis 3 contributor stats weeks[]; if stats 202 persists after retries, 9A unavailable — axis scores on remaining 3 sub-signals with confidence -0.2; report must note "sub-signal 9A unavailable — contributor stats computing"
- **Squash-merge repos**: commit substance ratio (9D) may undercount dep-bumps when PRs squash-merged under generic titles — known limitation; note if detected (large PR count, low distinct commit count relative to PR count)
- **New repos (\<12m old)**: reviewer pool drift (9A) requires weeks[-52:-26] baseline; repos younger than ~12m have sparse/empty prior-6m window → pool_prior empty → 9A ⚪; axis scores on remaining 3 sub-signals; not a bug
- **High-volume dep-bump repos**: 9D 🔴 = correct signal human commit output is maintenance-only; combined with 9A stable pool may indicate healthy automated maintenance — note both signals in Key Signal column
- **Dependabot manifest_path misclassification**: any alert whose `manifest_path` matches `*_test.txt` classified as runtime = **B2 scoring bug**; validate by grepping `dependabot.json` for `segmentation_test.txt`, `classification_test.txt`, `*_test.txt` entries — those must never appear in user-exposure severity count
- **CODEOWNERS active count from memory**: if active maintainer list includes any login with 0 commits in `commits.json` window, or omits a login with ≥1 commits who appears in CODEOWNERS = **B3 data bug**; must recompute programmatically on every run
- **CI pass-rate denominator trim**: Axis 5 CI pass-rate using trimmed denominator (e.g. 9/10 when total runs = 21) = **B4 methodology bug**; correct = 10/21; validate: pass-rate denominator must equal `len(workflow_runs.json)`
- **Truncation phantom**: if open-issue stale-rate body says "N-cap window" but `len(open_issues.json) == open_issues_count - open_prs_count`, body contradicts Gaps section = **B5 framing bug**; validate consistency between body and Gaps section
- **Cross-version score comparison**: comparing Health Score from v0.6.x run vs v0.7.x run without version annotation = **B6 methodology bug**; axis weights differ across minor versions; scores not directly comparable without version qualifier

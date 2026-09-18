<!-- file: vitality-scoring-group-unassigned.md — consumers: oss/agents/repo-warden.md -->

# Vitality Scoring Rubrics — Group UNASSIGNED

> Axes 10-13 — not yet wired to any repo-warden group (see Implementation Status in vitality-scoring.md); scoring ⚪ until gh-scraper fetch groups implemented
>
> Split from `vitality-scoring.md` (see that file for Weights & Confidence Thresholds table, Advisory Signals, Implementation Status).

## Axes

### Axis 10 — Supply-Chain Integrity

(entirely absent from prior design, post-XZ-backdoor critical gap, OpenSSF Risk: High for all sub-checks)

**Data requirements** (new gh-scraper fetches needed — see Phase 2 note at file end):

- Full workflow YAML content for all `.github/workflows/*.yml` files (Axis 5 fetches presence only; Axis 10 needs full YAML text)
- Release assets list: `GET /repos/{owner}/{repo}/releases?per_page=10` → `assets[].name` per release
- Workflow run names already fetched (Axis 5) — reuse for checkpoint 5 grep

5 checkpoints:

1. **Action pin discipline** — `uses:` lines in all workflow YAMLs SHA-pinned (not `@v1`, `@main`, `@master`, `@latest`). Pattern: `uses:\s+\S+@[a-f0-9]{40}` = SHA-pinned. Pass: ≥80% of all `uses:` lines across all workflows are SHA-pinned. (Score: met / max)
2. **Least-privilege token permissions** — no workflow file contains `permissions: write-all`; all workflow files have an explicit top-level `permissions:` block (not absent). Pass: all workflow files satisfy both conditions. Note: `permissions: {}` (empty) is NOT a pass — empty block grants no permissions but is intentional only if job-level perms exist; score it as 🟡 case.
3. **Dangerous patterns absent** — no workflow step contains: (a) `pull_request_target` trigger combined with `ref: ${{ github.event.pull_request.head.ref }}` or equivalent head-ref checkout; (b) `curl | bash`, `curl | sh`, `wget | sh`, `wget | bash` in `run:` steps. Pass: none of these patterns detected in any workflow file.
4. **Signed releases** — last 5 releases (or all releases if \<5 exist) have at least one of: release asset matching `*.sig`, `*.asc`, `*.minisig`, `*.sigstore`, `*.bundle`; OR workflow step contains `sigstore/cosign-installer`, `actions/attest-build-provenance`, `slsa-framework/slsa-github-generator`. Pass: ≥60% of sampled releases satisfy at least one condition.
5. **SBOM published** — any release asset matches `*.sbom.json`, `sbom.spdx`, `*.cdx.json`, `*.spdx.json`; OR workflow step contains `anchore/sbom-action`, `microsoft/sbom-tool`, `CycloneDX`. Pass: SBOM generation step found in any workflow OR SBOM asset in last release.

Score: `floor(checkpoints_met / 5 × 10)`; 🟢 ≥4/5 | 🟡 2–3/5 | 🔴 ≤1/5

**403 / access failure handling**:

- Workflow content fetch 403 or decode error → checkpoints 1, 2, 3 = ✗; confidence -0.3
- Releases 403 or 0 releases → checkpoints 4, 5 = ✗; confidence -0.2
- GitHub Actions not used (no `.github/workflows/`) → checkpoints 1–3 = ✗ (no actions to pin); note in report; confidence unchanged

**Axis 10 confidence:**

- Base: 1.0
- -0.3 if workflow content unreadable (checkpoints 1–3 indeterminate)
- -0.2 if releases API 403 or \<2 releases (checkpoints 4–5 unreliable sample)
- -0.1 if \<3 releases sampled (signed-releases pass-rate unstable)
- Floor: 0.3

### Axis 11 — Ecosystem Criticality & Reach

(signals how embedded project is in dependency graph; changes interpretation of other axis scores — 🟡-health package with 100k dependents deserves different urgency than 🟡 hobby project)

**Data requirements** (new gh-scraper fetches needed — see Phase 2 note):

- GitHub dependency network: `GET https://github.com/{owner}/{repo}/network/dependents` HTML parse → dependent repository count (no official API; use Accept: `application/json` undocumented endpoint OR deps.dev REST: `GET https://api.deps.dev/v3alpha/systems/{system}/packages/{name}/dependents` if package known)
- Package registry presence: root-contents already fetched — detect `package.json`, `pyproject.toml`/`setup.py`, `Cargo.toml`, `go.mod`; then query registry for weekly downloads: PyPI `https://pypistats.org/api/packages/{name}/recent?period=week`, npm `https://api.npmjs.org/downloads/point/last-week/{name}`
- Stars and forks already in repo metadata (Group 1 fetch)

Computation:

- `dependent_repos` = parsed dependent repository count (⚪ if API unavailable after 2 retries)
- `registered_package` = True if package found on any of: PyPI, npm, crates.io, pkg.go.dev
- `weekly_downloads` = weekly download count from registry (⚪ if registry does not expose downloads API)
- Stars and forks from repo metadata

Score — **impact tier** (not a health failure; 🔴 = low-impact, not broken):

- 🟢: `dependent_repos ≥ 100` OR (`registered_package` AND `weekly_downloads ≥ 10 000`) OR (`stars ≥ 1 000` AND `registered_package`)
- 🟡: `dependent_repos 10–99` OR (`registered_package` AND `weekly_downloads 1 000–9 999`) OR `stars 100–999`
- 🔴: `dependent_repos < 10` AND NOT(`registered_package` AND `weekly_downloads ≥ 1 000`) — niche or personal project
- ⚪: neither dependents API nor registry available (cannot compute; axis excluded from weighted score)

**Report framing**: always surface as "Ecosystem impact: low / moderate / high" alongside emoji — never phrase 🔴 here as a health failure. Annotate: "🔴 = low downstream impact, not a quality signal."

**Axis 11 confidence:**

- Base: 1.0
- -0.4 if dependent_repos = ⚪ AND weekly_downloads = ⚪ (score derived only from stars/registry presence — very indirect)
- -0.2 if dependent count API returned inconsistent pagination (partial count)
- -0.1 if only one registry checked (monolingual; other ecosystems unchecked)
- Floor: 0.3

### Axis 12 — Dependency Health (Libyears)

(current design only checks "Dependabot config present", no actual dep freshness signal; leading indicator where Axis 8 Dependabot alerts lagging)

**Data requirements** (new gh-scraper fetches needed — see Phase 2 note):

- Package manifest files via GitHub contents API: `requirements.txt`, `pyproject.toml`, `setup.cfg`, `setup.py`, `package.json`, `package-lock.json`, `Cargo.toml`, `Cargo.lock`, `go.mod`, `go.sum`; parse pinned version per dep
- Per-dependency latest release date from registry:
  - PyPI: `GET https://pypi.org/pypi/{name}/json` → `releases.{v}.upload_time` for pinned version; `info.version` for latest
  - npm: `GET https://registry.npmjs.org/{name}/{pinned_version}` → `_time`; `GET https://registry.npmjs.org/{name}/latest` → `version`
  - crates.io: `GET https://crates.io/api/v1/crates/{name}/versions` → `created_at` per version
  - Go: `GET https://proxy.golang.org/{module}/@v/{version}.info` → `Time`
- Lock file presence: `poetry.lock`, `package-lock.json`, `yarn.lock`, `Cargo.lock`, `go.sum`, `uv.lock`, `pdm.lock`

Computation:

- For each direct dependency `d` with pinned version `v_pinned`:
  - `release_date_pinned` = upload date of `v_pinned` from registry
  - `release_date_latest` = upload date of current latest stable version
  - `libyears_d` = (`release_date_latest` − `release_date_pinned`) / 365.25
- `median_libyears` = median across all successfully resolved deps
- `max_libyears` = max (sentinel for severely outdated single dep)
- `outdated_ratio` = count(deps where `libyears_d > 0.5`) / total_resolved_deps (>0.5 = more than 6 months behind)
- `lock_file_present` = True if any lock file found in root-contents
- Skip: dev-only deps (`*-dev`, test extras) unless no production deps found

Score:

- 🟢: `median_libyears < 0.5` AND `outdated_ratio < 25%` AND `lock_file_present`
- 🟡: `median_libyears 0.5–2.0` OR `outdated_ratio 25–60%` OR `not lock_file_present`
- 🔴: `median_libyears > 2.0` OR `outdated_ratio > 60%` OR `max_libyears > 5` (critical dep severely outdated)
- ⚪: no package manifest found in root; OR ecosystem not PyPI/npm/crates.io/Go (no Libyears API available); OR \<3 deps resolved (sample too small for median)

**Axis 12 confidence:**

- Base: 1.0
- -0.2 if \<5 deps resolved (small sample; median unstable)
- -0.2 if ≥1 registry API returned 429/503 (partial data; some deps unresolved)
- -0.3 if transitive deps not included (direct-only; total Libyears may be underestimated)
- -0.1 if lock file absent (pinned versions from manifest may differ from installed)
- Floor: 0.3

### Axis 13 — Interface Stability & Community Engagement

(two grouped signal sets, each scored 0–5; axis score = sum 0–10; split enables separate reporting of stability vs community health, keeps combined weight light)

#### Group A — Interface Stability (scored 0–5)

**Data requirements** — reuses existing fetches:

- Last 50 commits (commit messages) — already in Group 1 data
- Releases list with body text — already in Group 1 data (last 10 releases)
- CHANGELOG file content — already fetched for Axis 6 checkpoint 4

Computation:

- `breaking_commits_365d` = count of commits in last 365d where message contains `BREAKING CHANGE:` or `!:` (conventional commits breaking indicator), or subject line contains `[breaking]` (case-insensitive)
- `major_bumps_365d` = count of releases in last 365d where version tag is `vN.0.0` with N > prior major (or `N.0.0` without v prefix)
- `semver_compliant` = True if every BREAKING CHANGE commit has a corresponding major version bump within 60 days (True also if `breaking_commits_365d = 0` — no breaking changes = compliant by definition)
- `deprecation_pattern` = True if: CHANGELOG or release notes contain both a "deprecated" entry AND a "removed"/"breaking" entry for at least one feature AND the "deprecated" entry appears in an earlier release than the "removed" entry (minimum 1 release gap)
- `migration_guide_ratio` = count(major releases with "migration" OR "upgrade guide" OR "upgrade from" in release body) / max(major_bumps_365d, 1)

Score A (0–5):

- 5: `semver_compliant` AND (`deprecation_pattern` OR `breaking_commits_365d = 0`) AND (`migration_guide_ratio ≥ 0.80` OR `major_bumps_365d = 0`)
- 4: `semver_compliant` AND `migration_guide_ratio ≥ 0.50`
- 3: `semver_compliant` but missing deprecation warnings or migration guides
- 2: `NOT semver_compliant` but `migration_guide_ratio ≥ 0.50` (communicated but process incomplete)
- 1: `breaking_commits_365d > 6` without `deprecation_pattern` or `migration_guide_ratio < 0.25`
- 0: BREAKING CHANGE commits with no corresponding major version bump AND no migration guidance in any affected release
- ⚪ (exclude from sum, reduce max to 5): no releases exist — cannot assess SemVer compliance

**Note**: high `breaking_commits_365d` is NOT automatically penalized — a project in active API evolution with SemVer compliance + migration guides may score 5 regardless. The axis rewards process, not stagnation.

#### Group B — Community Engagement (scored 0–5)

**Data requirements** — mostly reuses existing fetches:

- Contributor stats weeks[] (already in Group 1 data, reused from Axis 3)
- Merged PR author logins (already in Group 1 data, reused from Axis 4)
- New fetch: GitHub Discussions count (GraphQL `repository.discussions(first:1) { totalCount }` — lightweight single-field query)

Computation:

- `top5_authors` = set of top-5 all-time commit authors by commit count (from contributor stats)
- `external_pr_ratio` = count(merged PRs last 90d where author NOT in `top5_authors`) / total merged non-bot PRs last 90d
- `new_contributors_90d` = distinct contributor logins active in stats weeks[-13:] but NOT active in stats weeks[-26:-13] (new faces in last 90d vs prior 90d)
- `active_contributors_90d` = distinct contributors with ≥1 commit in stats weeks[-13:]
- `contributor_acquisition_rate` = new_contributors_90d / max(active_contributors_90d, 1)
- `discussion_count_90d` = GitHub Discussions opened in last 90d (from new GraphQL query; ⚪ if repo has Discussions disabled)

Score B (0–5):

- 5: `external_pr_ratio ≥ 0.40` AND `new_contributors_90d ≥ 3` AND `contributor_acquisition_rate ≥ 0.20`
- 4: `external_pr_ratio ≥ 0.25` AND `new_contributors_90d ≥ 1`
- 3: `external_pr_ratio ≥ 0.10` OR `new_contributors_90d ≥ 1`
- 2: external_pr_ratio < 0.10 but `discussion_count_90d ≥ 5` (community engages via discussions, not PRs)
- 1: zero external contribution but ≥1 issue from non-contributor in last 90d
- 0: all contributions single person, zero external engagement of any kind
- ⚪ (exclude from sum, reduce max to 5): repo \<6 months old (too early to assess community formation) OR contributor stats API 202 with no fallback

**Axis 13 overall score** = Score_A + Score_B (0–10)

- 🟢: ≥7
- 🟡: 4–6
- 🔴: ≤3
- ⚪: BOTH groups ⚪ simultaneously (new repo with no releases and no activity data)

When one group is ⚪: score = remaining group score × 2 (normalize to 0–10); reduce confidence -0.3; note in report which group was unavailable.

**Axis 13 confidence:**

- Base: 1.0
- -0.3 if Group A ⚪ (no releases — interface stability not assessable)
- -0.3 if Group B ⚪ (repo too young — community engagement not assessable)
- -0.2 if contributor stats 202 with fallback used (acquisition rate approximate)
- -0.1 if Discussions API unavailable or disabled (discussion_count_90d unknown)
- -0.1 if \<5 merged PRs in 90d (external_pr_ratio sample unstable)
- Floor: 0.3

# 🌱 oss — Claude Code Plugin

OSS workflow plugin for Python/ML open-source projects. Four agents (two user-facing, two internal pipeline) and five slash-command skills: issue analysis, parallel code review, PR resolution, release artifacts/readiness, and post-install rule setup.

Public actions stay maintainer-owned: replies, merges, pushes, tags, and releases are drafted or prepared here, never posted or published automatically.

Optional Codemap index-gate guidance ships with oss, so loading it does not depend on another plugin's private shared directory. Structural queries still require the `codemap-py` plugin; an unavailable CLI or local contract retains the file-read fallback.

> Works standalone — `foundry` not required. Without it, agent dispatches fall back to `general-purpose` with role descriptions and less specialization. Installing `foundry` unlocks the specialized agent roster.

______________________________________________________________________

<details>

<summary><strong>📋 Contents</strong></summary>

- [What is oss?](#-what-is-oss)
- [Why oss?](#-why-oss)
- [Install](#oss-install)
- [Quick start](#-quick-start)
- [Skills reference](#-skills-reference)
  - [/oss:analyse](#ossanalyse)
  - [/oss:review](#ossreview)
  - [/oss:resolve](#ossresolve)
  - [/oss:release](#ossrelease)
  - [/oss:setup](#osssetup)
- [Agents reference](#-agents-reference)
  - [oss:gh-scraper](#gh-scraper)
  - [oss:repo-warden](#repo-warden)
  - [oss:shepherd](#ossshepherd)
  - [oss:cicd-steward](#osscicd-steward)
- [Configuration](#-configuration)
- [Bin helper inventory](#bin-helper-inventory)
- [Troubleshooting](#troubleshooting)
- [Contributing / feedback](#contributing--feedback)

</details>

______________________________________________________________________

## 🤔 What is oss?

`oss` = Claude Code plugin for Python/ML open-source maintainers. Five slash-command skills — analyse, review, resolve, release, and setup — plus four agents for contributor communication, CI health, and vitality data collection/scoring. It covers recurring maintainer work: triaging GitHub threads, multi-perspective PR review, organizing review feedback into fixes, and preparing release artifacts with readiness checks.

______________________________________________________________________

## 🎯 Why oss?

Maintaining OSS = three competing demands: review code carefully (catch regressions), respond to contributors fast (keep engaged), ship releases confidently (users upgrade). Each = context-switch tax.

`oss` targets that tax:

**Reduce review context switching.** `/oss:review` classifies the PR, runs relevant review dimensions in parallel, and writes a ranked report. The default fan-out is capped at four scope-selected dimensions; `--full` runs every dimension selected by scope. An optional Codex integration can add a co-review, and missing optional agents fall back gracefully.

**Contributors get a usable response draft.** The `--reply` flag drafts a welcoming comment in project voice, citing project conventions. The draft is written for maintainer review; the plugin does not post it automatically.

**Turn review feedback into traceable action items.** `/oss:resolve` closes the gap between "reviewer said X" and "X in code." It reads live PR comments, a saved review report, or both; deduplicates sources; resolves conflicts semantically; and implements selected items with `[resolve No.N]` attribution, using isolated worktrees for specialist batches.

**Release communication stays grounded in the diff.** `/oss:release` classifies changes, checks documentation/version consistency, writes release notes and optional changelog/summary/migration artifacts, and audits readiness. It does not edit package versions, create tags, or publish packages.

**Triage with structure.** `/oss:analyse vitality` produces a repo vitality scorecard with duplicate issue clustering and stale-PR detection. A specific thread becomes a structured summary with next actions.

______________________________________________________________________

<a id="oss-install"></a>

## 📦 Install

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install oss@borda-ai-rig
```

After installation, run `/oss:setup` once to link this plugin's rules into `~/.claude/rules/`; use `/oss:setup --approve` for the non-interactive path. Re-run it after an upgrade. In this repository, `make sync-claude` invokes that setup path for managed installs.

Recommended setup:

```bash
claude plugin install foundry@borda-ai-rig   # specialized agents; optional
```

`oss` requires Claude Code, Python 3.10+, and an authenticated GitHub CLI (`gh auth login`) for GitHub-backed workflows. The plugin can run without `foundry`, but agent calls then use `general-purpose` fallbacks with lower specialization.

**Optional integrations** (unlock extra capabilities inside `oss` skills):

| Plugin                | What it unlocks                                                                                                                                                                                      |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `foundry`             | Specialized review and implementation agents instead of `general-purpose` fallbacks                                                                                                                  |
| `bridge@borda-ai-rig` | Optional Codex co-review in `/oss:review` and Codex action-item dispatch in `/oss:resolve`                                                                                                           |
| `codemap-py`          | Reverse-dependency count (`rdep_count`) in `/oss:review` risk assessment; stale-symbol detection in `/oss:analyse` issue triage, Open-PR Overlap + Structural Constraints in `/oss:analyse vitality` |

All `oss` skills degrade gracefully when optional plugins absent — reduced capability, not broken commands.

Install the optional Codex integration only if you need those paths:

```text
/plugin install bridge@borda-ai-rig
/reload-plugins
```

For codemap-backed structural signals, install `codemap-py` from the same marketplace:

```bash
claude plugin install codemap-py@borda-ai-rig
```

For local-file review, install `develop` and use `/develop:review` (requires `develop` plugin); `/oss:review` is for GitHub pull requests.

> **Note:** Skills always use the `oss:` prefix: `/oss:analyse`, `/oss:review`, `/oss:resolve`, `/oss:release`, and `/oss:setup`.

<details>

<summary><strong>Upgrade</strong></summary>

```bash
claude plugin install oss@borda-ai-rig
```

</details>

<details>

<summary><strong>Uninstall</strong></summary>

```bash
claude plugin uninstall oss
```

</details>

______________________________________________________________________

## ⚡ Quick start

```text
# Morning: understand what needs attention
/oss:analyse vitality

# Review top PR and draft a contributor-facing response
/oss:review 55 --reply

# Apply selected review feedback
/oss:resolve 55 report

# Prepare release artifacts
/oss:release prepare v2.1.0
```

______________________________________________________________________

## 🔧 Skills reference

### /oss:analyse

Analyse GitHub threads + repo vitality. Accepts issue/PR number, keyword `vitality`, keyword `ecosystem`, or path to saved report file.

**Purpose:** Structured actionable summary of any GitHub thread, or broad view of open work. No need to read every comment yourself.

**Auto-invokes when:** user gives GitHub issue/PR number (`#N`) or `github.com` URL + asks analyze/summarize/triage; user asks "is this repo healthy" or vitality stats.

**Invocation:**

```text
/oss:analyse 123                # issue, PR, or discussion by number
/oss:analyse vitality           # repo vitality: 9-axis health scorecard, duplicate clustering, raw data JSONL
/oss:analyse vitality --quick   # fast daily scorecard: core scoring only, skips codex + adversarial passes
/oss:analyse ecosystem          # dependency health, upstream compatibility
/oss:analyse path/to/report.md  # re-analyse a saved report
```

**Flags:**

| Flag               | Effect                                                                                                                                                                                      |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--reply`          | Draft contributor-facing response after analysis (routed through `oss:shepherd` for voice consistency)                                                                                      |
| `--quick`          | Vitality only: fast daily scorecard — skips Codex independent review + adversarial rework loop, reduces to 4 spawns. Full (reviewed) mode = default; confidence capped lower in quick mode. |
| `--keep "<items>"` | Append quoted string to compaction contract's `preserve:` line — items survive auto-compact mid-run (advanced; long multi-agent sessions)                                                   |

**What it does:**

Thread number: fetches issue/PR, reads all comments, classifies thread type (bug report, feature request, question, duplicate, stale), produces structured summary: what asked, current state, action needed from you, and — with `--reply` — draft response. With codemap index present, symbols/modules named in issue thread existence-checked against index; identifiers that no longer resolve flag issue as referencing stale code (with likely rename target when codemap suggests one).

`vitality`: pulls open issues + PRs, scores repo across 9 axes (Axes 1–8 plus Axis 9 Trajectory), clusters duplicates, flags threads stale beyond project threshold, gives prioritised triage list with weighted Health Score %. All raw API data saved to JSONL file alongside report for manual inspection. With codemap index, report gains Open-PR Overlap note (pairwise changed-file overlap plus tightly-coupled-module conflict candidates) and Structural Constraints block (highest-blast-radius modules, collision/degraded/stale index signals). Every codemap signal optional — no index or plugin absent → analyse degrades to GitHub-only behavior, flags gap inline, never blocks.

**Sample vitality scorecard (terminal output):**

```text
# Repo Vitality — example/mylib
**Skill:** oss:analyse v0.7.0 · **Generated:** 2026-05-11T10-00-00Z

---

## Executive Summary

Project is in healthy condition (74%) with strong CI/CD and documentation.
Bus factor of 2 is the primary risk. Dependency update config absent.

**Health Score:** 74% 🟡 · 5 healthy · 3 warning · 1 critical · 0 unavailable (⚪)
**Top Risk:** Axis 3 bus factor = 2 — one departure stalls merges

---

| # | Axis                 | Score  | Status | Key Signal                           |
|---|----------------------|--------|--------|--------------------------------------|
| 1 | Responsiveness       | 10.0   | 🟢     | median issue 1.2d, PR 0.8d; 94% ≤7d |
| 2 | Maintenance activity | 10.0   | 🟢     | last commit 3d, 18 commits/30d       |
| 3 | Contributor health   |  5.0   | 🟡     | bus factor 2, retention 67%          |
| 4 | Issue & PR health    |  5.0   | 🟡     | stale 18%, close 0.71, review 62%    |
| 5 | CI/CD & code quality | 10.0   | 🟢     | 5/5 checks, CI pass 95%              |
| 6 | Documentation        |  7.8   | 🟢     | 7/9 checkpoints                      |
| 7 | Governance           |  8.3   | 🟢     | 5/6 files, active maint 3/3          |
| 8 | Security posture     |  5.0   | 🟡     | dep-config: no, alerts: 403          |
| 9 | Trajectory           |  5.0   | 🟡     | pool -10%, TTM 2d->3d, P90 45d, dep 12% |
|   | **Total Score**      | **74%** |       |                                      |
```

**Output locations:**

- Thread analysis: `.reports/analyse/thread/`
- Vitality report: `.reports/analyse/vitality/`
- Ecosystem report: `.reports/analyse/ecosystem/`

GitHub API responses cached in `.cache/gh/` by number and date (30-day TTL) — repeated calls on same thread fast.

______________________________________________________________________

### /oss:review

Scope-aware parallel review of a GitHub PR. Input is a PR number or a saved review report for reply drafting.

**Purpose:** Review architecture, tests, performance, docs, linting, and security in parallel. Produce a ranked findings report and, optionally, a welcoming contributor comment.

**Invocation:**

```text
/oss:review 55                # scope-aware review — saves findings report
/oss:review 55 --reply        # review + draft contributor-facing comment
```

> Local files or current git diff without PR → use `/develop:review` from `develop` plugin.

**How the pipeline works:**

```text
Tier 0  git diff --stat
        Scope detection — exits only if no Python or doc files changed

        Acceptance gate — Stage 1 (reject, terminal) then Stage 2 (block,
        non-terminal); reads PR description + known CI status before any
        agent spawns. Full criteria: "Review stages" below.

Optional Codex co-review
        Runs when `bridge@borda-ai-rig` is installed and enabled; otherwise skipped

Tier 2  Parallel review dimensions
        PR metadata/diff/checks fetched once at Step 0 (per-run snapshot) and reused by every stage
        Scope selects spawn units — paired dimensions share one agent
        (perf+architecture in one opus spawn, docs+lint in one sonnet spawn),
        each writing its own per-dimension finding file + confidence block
        Default ranks units under a cap of three; qa-specialist is pinned
        outside the cap (security scan runs on every code PR)
        `--full` runs every unit selected by scope
        Without foundry, requested agents use general-purpose fallbacks

        Scope examples:
          docs-only PR → foundry:doc-scribe (+ challenge/Codex when enabled/available)
          docs + CI PR → oss:cicd-steward + foundry:doc-scribe (+ challenge/Codex)
          tests + CI   → foundry:qa-specialist + foundry:linting-expert
          code PR      → pinned qa-specialist + top-ranked units from code, perf+architecture, docs+lint, challenge

        CI status: failing CI noted in report header — review always proceeds
        codemap integration: rdep_count > 20 flags as high-risk change

        Consolidation: the selected consolidator merges findings into a ranked report

        --reply: oss:shepherd drafts contributor-facing comment from consolidated report (written to .temp/; user posts)
```

**Review stages (acceptance gate):** `Gate:` header field is two states beyond `PASS` — reject is terminal (skips every tier, no agent spawned), block is not (full fanout still runs, the report just surfaces the fixable gap up front instead of burying it after N findings). Test: *could revising the code, not the goal, resolve this?* Yes → block. No → reject.

<details>
<summary><strong>Stage 1 — Reject (terminal, 8 grounds)</strong></summary>

Aligned with close-without-merge practice in K8s/CPython/Rust/Django contributing docs. Every ground needs affirmative evidence, never suspicion alone.

| Ground              | Test                                                                                                                                                                                                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REJECT_GOAL`       | Stated goal factually/technically wrong regardless of diff quality — e.g. "raise this [0,1]-bounded metric above 1.0". Disagreeing with the approach is a normal review finding, not this.                                                                                |
| `REJECT_CONDUCT`    | By-design adversarial/malicious contribution or Code of Conduct violation. Confirmed via a shared `foundry:challenger` check before rejecting — accidental (or confidence \<0.7) falls through as a normal finding.                                                       |
| `REJECT_SCOPE`      | Out of project scope / against roadmap, maintainers already decided against this direction. Evidence: a `wontfix`/`invalid`/`declined`/`out-of-scope` label already applied, or an explicit "out of scope" statement in `CONTRIBUTING.md`/an ADR the PR directly matches. |
| `REJECT_LICENSE`    | Incompatible license copied in, or plagiarized/copied source with no right to submit it. Not the same as a missing CLA/DCO signature (that's Stage 2, fixable). Confirmed via the same shared `foundry:challenger` check as conduct.                                      |
| `REJECT_DUPLICATE`  | Another PR already merged solving this, or the linked issue already fixed upstream. Evidence: the linked issue is closed by a different, already-merged PR.                                                                                                               |
| `REJECT_REVERTED`   | Reintroduces a previously reverted change without addressing why it was reverted. Evidence: a matching revert commit exists **and** the PR body doesn't reference or address it.                                                                                          |
| `REJECT_SPAM`       | Spam/low-effort/AI-slop — no real change, hacktoberfest-farming pattern. Evidence needs both a trivially low-value diff **and** a generic/templated description — either alone isn't enough (a genuine one-line fix looks low-value too).                                 |
| `REJECT_PHILOSOPHY` | Contradicts a documented design principle, not just a style preference — e.g. adding a GUI to a project whose docs state "CLI-only by design". Requires a citable doc line.                                                                                               |

</details>

<details>
<summary><strong>Stage 2 — Block (non-terminal, full review still runs)</strong></summary>

Default `[blocking]` tag per finding category — judgment still required, not automatic:

| Category                                                                   | Default                                        | Nuance                                                                                                                                                                                             |
| -------------------------------------------------------------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CI red / failing check                                                     | blocking                                       | Only a **major**/required-check failure. A single flaky-looking rerun blip is noted, not auto-blocking.                                                                                            |
| Missing test coverage for new/changed logic                                | blocking                                       | —                                                                                                                                                                                                  |
| Accidental security bug (careless, not by-design)                          | blocking                                       | By-design version is Stage 1 `REJECT_CONDUCT`, not this.                                                                                                                                           |
| Breaking API change, no deprecation/migration path                         | blocking                                       | —                                                                                                                                                                                                  |
| Missing docs for new/changed public behavior                               | blocking                                       | Missing CHANGELOG entry alone is **not** blocking — can land in a follow-up (`/oss:release`).                                                                                                      |
| Perf regression                                                            | contextual                                     | A regression vs recent releases with no offsetting reason is bad; not blocking when the prior speed only existed because of a correctness bug and the "regression" is the cost of fixing it right. |
| Merge conflicts                                                            | **not** blocking                               | `/oss:resolve`'s job — review doesn't gate on it.                                                                                                                                                  |
| Incomplete implementation (TODOs in changed paths, missing error handling) | blocking                                       | —                                                                                                                                                                                                  |
| Missing CLA/DCO signature                                                  | blocking, **only if the project requires one** | Check first — CLA-assistant/DCO-check bot status, or a signing mandate in `CONTRIBUTING.md`. No such requirement → not applicable.                                                                 |

</details>

**Typical scenarios:**

| PR type                | Agents that run                                                           | Skipped                                          |
| ---------------------- | ------------------------------------------------------------------------- | ------------------------------------------------ |
| Docs-only (.md, .rst)  | doc-scribe, plus challenge/Codex when enabled/available                   | Code, test, performance, architecture dimensions |
| Docs + CI/CD           | oss:cicd-steward, doc-scribe, plus challenge/Codex when enabled/available | Code, test, performance, architecture dimensions |
| Tests + CI             | qa-specialist, linting-expert                                             | Other dimensions                                 |
| Annotation-only Python | linting-expert                                                            | Other dimensions                                 |
| Code PR                | Pinned qa-specialist + top three ranked units (`--full` removes the cap)  | Units ruled out by scope or the default cap      |

Without `foundry`, selected dimensions fall back to `general-purpose` agents with role descriptions — functional, but less specialized.

**Output locations:**

- Per-agent handover files (intermediate): `.temp/review/<timestamp>/`
- Consolidated report: `.reports/review/<timestamp>/review-report.md`
- Reply draft (with `--reply`): `.temp/output-reply-<PR#>-<date>.md`

**Flags:**

| Flag                         | Effect                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--reply`                    | After consolidation, `oss:shepherd` drafts a welcoming two-part PR comment (positive framing first, then specific actionable asks)                                                                                                                                                                                                                                                                                                                      |
| `--no-challenge`             | Skip the adversarial challenge pass                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `--codemap` / `--no-codemap` | Require or disable codemap structural context; default is automatic when available                                                                                                                                                                                                                                                                                                                                                                      |
| `--semble`                   | Enable the optional semble semantic-search companion                                                                                                                                                                                                                                                                                                                                                                                                    |
| `--full`                     | Run every dimension selected by scope instead of the default top-four cap                                                                                                                                                                                                                                                                                                                                                                               |
| `--worktree`                 | Opt-in. Run the review in an isolated git worktree (base: HEAD) so no dimension agent can mutate main sources. Report is written to the **main tree**; you review + merge. PR-review mode only (not `--reply`/direct-report). On entry it also reports leaked worktrees it could reclaim (clean, ≥14 d, `agent-*`/`oss-*`, including ones git no longer has registered) and asks before deleting any — trees with uncommitted work are kept at any age. |
| `--keep "<items>"`           | Append quoted string to compaction contract's `preserve:` line — items survive auto-compact mid-run (advanced; long multi-agent sessions)                                                                                                                                                                                                                                                                                                               |

______________________________________________________________________

### /oss:resolve

Apply review findings to codebase. Reads live PR comments, saved review report, or both — deduplicates, resolves conflicts, implements fixes.

**Purpose:** Close gap between "reviewer said X" and "X in code." One command: open findings → committed fixes.

**Reject-gate interlock:** if the newest `/oss:review` report for this PR carries `Gate: REJECT_<GROUND>` (any of the 8 grounds under "/review" above), resolve refuses to start unless the PR's head commit has changed since that review — a rejected premise isn't something a code fix resolves. `Gate: BLOCK` (e.g. red CI) and everything else proceed normally — resolve is exactly the fix path for those.

**Invocation:**

```text
/oss:resolve 55                # pr mode — apply fixes from live GitHub PR comments
/oss:resolve report            # report mode — apply fixes from the saved /oss:review report
/oss:resolve 55 report         # pr + report mode — both sources, deduplicated
/oss:resolve                   # review-handoff mode — picks up from the last /oss:review run
```

**Source modes:**

| Arguments        | Mode           | Source                            | When to use                                                 |
| ---------------- | -------------- | --------------------------------- | ----------------------------------------------------------- |
| `55` (PR number) | pr             | Live GitHub PR comments           | Apply feedback posted directly on GitHub                    |
| `report`         | report         | Saved `/oss:review` findings file | Apply findings from last review run                         |
| `55 report`      | pr + report    | Both, aggregated                  | Full close — deduplicates across both inputs                |
| _(none)_         | review-handoff | Review-handoff                    | Continues directly from last `/oss:review` run this session |

`report` / review-handoff discovery looks for the newest report across both `.reports/review/*/review-report.md` (`/oss:review`'s own output) and `.reports/codex/review/*/review-notes.md` (a Codex-native review run outside this plugin). Only the former's section schema is parsed — a codex-lineage report is detected and refused with an explicit message (rather than silently skipped or mis-parsed) so a blocking finding never goes unactioned; pass the PR number explicitly instead.

**How it works:**

Three phases:

1. **Intelligence gathering** — dedicated subagent fetches full PR thread (comments, reviews, inline code comments) — orchestrator context stays small; subagent classifies each finding, writes structured output to files; orchestrator reads compact classified table
2. **Conflict resolution** — merge conflicts: read intent from both sides; apply semantically correct resolution (never mechanical "take ours"/"take theirs")
3. **Action item implementation** — three-phase parallel dispatch: medium-effort items go to Codex first, batched up to three disjoint-file items per call (same-file items stay sequential; per-item verdicts and commits preserved); everything else splits into Phase 1 challenge (grouped by domain, all groups fire concurrently — read-only, safe to overlap), Phase 2 implementation (grouped by one of six specialists — `sw-engineer`, `qa-specialist`, `doc-scribe`, `linting-expert`, `perf-optimizer`, `solution-architect` — max 5 items/group, each group runs in its own isolated `git worktree` so concurrent specialists never race on the same working tree), Phase 3 merge-back (orchestrator cherry-picks every item's commit onto the PR branch, sequentially — whole worktree groups ordered most-central-first, so a foundational contract change lands before the commits that depend on it). Before Phase 2 dispatch, two grouping tiebreaks reduce Phase 3 conflicts at the root: a **file-ownership** tiebreak (rank: `linting-expert < doc-scribe < qa-specialist < perf-optimizer < sw-engineer < solution-architect`) reassigns every item touching a contested file to its single highest-ranked owner, so two specialists never edit the same file concurrently; then a soft **import-coupling** merge co-locates items in different files when one imports the other (caught via codemap forward `deps` — a module's own imports, so recall isn't truncated by the caller-list cap), so a rename and its callers land in one worktree instead of silently diverging. The codemap maps are built once, concurrently with the read-only challenge phase, so grouping adds ~0 wall-clock. Any conflict that still slips through Phase 3 routes through the same semantic conflict-resolution as Step 5. A branch mutex blocks a second concurrent resolve run from racing the same tree, and a HEAD fingerprint taken before Phase 2 flags any external write that lands mid-flight so cherry-picks never stack silently on a moved base. Per-item verdicts and `[resolve No.N]` attribution unchanged throughout; soft cap 10 items per dispatch (AskUserQuestion beyond, hard cap 20); soft codemap blast-radius check flags callers of changed modules before Phase 2 dispatch

Resolve after `/review` on same PR: blast-radius check reuses per-module codemap answers review already computed, no re-query — review's persisted pre-flight batch split into freshness-stamped per-module artifacts. Freshness is fail-closed on three conditions, all of which must hold: `prefix.git_sha` matches the current index `git_sha`; `prefix.scanned_at` is not older than the index's (a rebuilt index invalidates every artifact); and `prefix.index_stamp` still equals the index file's `<size>:<mtime_ns>`. That stamp is what makes the rule hold without trusting index-declared metadata — an `--incremental` re-scan, a restored backup, or a manual edit can leave `git_sha` and `scanned_at` untouched, and the first two checks alone would not see it. An artifact written before the stamp field existed carries none and is re-queried rather than trusted. Verdict reasons from `codemap_cache.py read`: `fresh` · `git_sha_mismatch` · `index_rebuilt` · `index_stamp_mismatch` · `content_hash_mismatch`. Reuse measured as `reuse_ratio` (fraction of persisted answers actually reused). No review artifact or codemap plugin absent → every module cache miss, scan queries live — no behaviour change.

**Severity → triage type mapping** (report mode):

| Review severity         | Section                                                        | Resolve `type`                                                           |
| ----------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------ |
| CRITICAL / `[blocking]` | any                                                            | `[req]`                                                                  |
| HIGH                    | any                                                            | `[req]`                                                                  |
| MEDIUM                  | Architecture, Performance, API Design (code-related)           | `[req]`                                                                  |
| MEDIUM                  | Test Coverage, Documentation, Static Analysis, Codex Co-Review | `[suggest]`                                                              |
| LOW                     | any                                                            | `[suggest]` — grouped by topic when count exceeds ceiling, never dropped |

`[req]` items apply by default on bulk-action; `[suggest]` items need explicit selection. Security findings inherit severity from `/oss:review` (hardcoded secrets → CRITICAL, dep CVEs → HIGH) — no separate security category. LOW findings cluster into composite rows by logical theme when total exceeds AskUserQuestion ceiling (12 items); each composite carries full member bullet list in `full_comment_text`.

**Guard rails:**

- More than 10 selected items → asks to batch or proceed with the slower larger dispatch; a single dispatch never exceeds 20 items
- More than 20 conflicted files → aborts, reports; you review manually
- Git push requires explicit confirmation before executing
- Core invariant: uses `git merge`, never `git rebase` — preserves history

Every resolve cycle closes with parallel `foundry:linting-expert` + `foundry:qa-specialist` passes before final report.

**Flags:**

| Flag                         | Effect                                                                                                                                                                                                                                                                                                                                                          |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--worktree`                 | Opt-in. Wrap the whole run in an isolated git worktree (base: HEAD) entered before `gh pr checkout`, so the checkout, per-specialist worktrees, and cherry-picks never touch your main tree/branch. Commits can push to the fork only after explicit confirmation; the local worktree is disposable. Composes with resolve's existing per-specialist isolation. |
| `--no-challenge`             | Skip per-item challenge validation                                                                                                                                                                                                                                                                                                                              |
| `--agent <name>`             | Select the implementation/intelligence agent instead of the default Codex path                                                                                                                                                                                                                                                                                  |
| `--codemap` / `--no-codemap` | Require or disable codemap structural context; default is automatic when available                                                                                                                                                                                                                                                                              |
| `--keep "<items>"`           | Append quoted string to compaction contract's `preserve:` line — items survive auto-compact mid-run (advanced; long multi-agent sessions)                                                                                                                                                                                                                       |

**Output location:** `.reports/resolve/<timestamp>/`

______________________________________________________________________

### /oss:release

Release communication and readiness pipeline with four modes: notes, prepare, audit, and demo.

**Purpose:** Prepare release artifacts from a verified change range and audit readiness before a human tags or publishes the release.

**Invocation:**

```text
/oss:release notes v1.2->HEAD                       # release notes from range
/oss:release notes --changelog                      # notes + CHANGELOG.md entry
/oss:release notes --summary                        # notes + internal summary
/oss:release notes v1.2->v2.0 --migration           # notes + migration guide
/oss:release notes --changelog --summary --migration  # all four outputs
/oss:release notes --append                         # integrate newly-landed commits into existing artifacts
/oss:release prepare v2.1.0                         # full pipeline: audit → all artifacts
/oss:release audit                                  # readiness check; does not tag or publish
/oss:release demo                                   # story-telling notebook for the release
/oss:release demo v1.2->v2.0                        # demo scoped to explicit range
```

Range notation: `v1->v2` (e.g. `v1.2->v2.0`). Omit range → defaults to `last-tag..HEAD`. Pre-release tags (`rcN`, `devN`, `alphaN`, `betaN`) excluded from tag detection automatically.

**Modes and flags:**

| Mode / Flag   | What it produces                                                                                                                                                                                                                                                            |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `notes`       | Release notes (`DRAFT.md`); add flags for extra outputs                                                                                                                                                                                                                     |
| `--changelog` | CHANGELOG.md entry (no shepherd review)                                                                                                                                                                                                                                     |
| `--summary`   | Internal summary saved to `.temp/`                                                                                                                                                                                                                                          |
| `--migration` | Migration guide for breaking changes saved to `.temp/` (shepherd review)                                                                                                                                                                                                    |
| `--append`    | Rerun the full pipeline scoped to newly-landed commits; integrate results into existing artifacts instead of a full regenerate (see below)                                                                                                                                  |
| `prepare`     | Full pipeline: audit → project `CHANGELOG.md` entry plus `HIGHLIGHTS.md`, `MIGRATION.md`, `SUMMARY.md`, `DRAFT.md`, and `demo.py` under `releases/<version>/`                                                                                                               |
| `audit`       | Readiness checklist: tests green, changelog present, version bumped, no uncommitted changes, doc proportionality for newly added features, no blocking upstream `/oss:review` verdict on file, changelog scope check , Codex adversarial pass (if `codex` plugin installed) |
| `demo`        | Story-telling jupytext notebook (`demo.py`) highlighting most significant contributions                                                                                                                                                                                     |

**What each mode does:**

| Primitive                                    | `notes`       | `prepare` | `audit` | `demo` |
| -------------------------------------------- | ------------- | --------- | ------- | ------ |
| Read git log + PRs                           | full          | diff      | full    | full   |
| Classify changes                             | ✓             | ✓         | -       | ✓      |
| Explore codebase                             | full          | diff      | full    | diff   |
| Shepherd voice review                        | ✓             | ✓         | -       | -      |
| DRAFT.md                                     | write         | write     | -       | -      |
| CHANGELOG.md                                 | `--changelog` | write     | -       | -      |
| SUMMARY.md                                   | `--summary`   | write     | -       | -      |
| MIGRATION.md                                 | `--migration` | write¹    | -       | -      |
| HIGHLIGHTS.md                                | -             | write     | -       | -      |
| demo.py                                      | -             | write²    | -       | write² |
| Release mode (informational, never blocking) | -             | ✓         | ✓       | -      |
| Working tree                                 | -             | ✓         | ✓       | -      |
| CI status                                    | -             | ✓         | ✓       | -      |
| Open issues / PRs                            | -             | ✓         | ✓       | -      |
| Docs alignment                               | -             | diff      | full    | -      |
| Version consistency                          | -             | ✓         | ✓       | -      |
| CVEs                                         | -             | ✓         | ✓       | -      |
| Upstream review verdict                      | -             | ✓         | ✓       | -      |
| Codex adversarial pass                       | -             | ✓         | ✓       | -      |

Flag mark = output produced only when flag passed. ¹ Full guide when breaking changes detected; single-line stub otherwise. ² Jupytext percent-format Python script with `# %%` code cells and `# %% [markdown]` narrative cells; self-contained with references to additional resources.

**Version and breaking-change checks:**

The pipeline checks version consistency and classifies public breaking changes when codemap evidence is available. It does not choose or write a package version, create a git tag, or upload to PyPI/another registry; perform those project-specific release steps separately after reviewing the generated artifacts.

**Breaking-change classification** (codemap-gated): after truth check, each diff-derived public symbol run through `fn-rdeps --exclude-tests`. Symbol with caller outside own top-level package → labelled **Breaking**, moved to ⚠ Breaking Changes with external call sites cited; symbol whose callers all inside own package stays under human label. Affected call-site list drafted into migration guide — downstream consumers see exactly what to change. Requires codemap v3 index — no index → phase skipped, human classification stands. Partial coverage (`query_complete:false`) surfaces evidence as possibly-incomplete, never drops it.

**CHANGELOG section ordering** (strict, enforced):

```text
Added → Breaking Changes → Changed → Deprecated → Removed → Fixed → 🔒 Security
```

**Deprecation tracking:** Uses `pyDeprecate` for deprecation lifecycle. Migration guides include before/after table with argument mapping for all renamed/removed parameters.

**Shepherd review** applies to release notes + migration guides. CHANGELOG entries + summaries written directly, no review.

**`--append`:** assumes an earlier `notes` run already produced `DRAFT.md` (and, when their flags were used, `CHANGELOG.md`/`SUMMARY.md`/`MIGRATION.md`) and reruns the full pipeline — unchanged, Gather changes through Draft executive summary — scoped to only the commits landed since then, tracked via a per-branch marker at `.temp/release-last-processed-<branch>`. Results integrate into every existing artifact in place via Read + Edit tool (no parsing script): DRAFT.md's Summary/Spotlights/Migration-guide/Notable-changes/Contributors sections all merge (not overwrite); root-level `SUMMARY.md`/`MIGRATION.md` merge the same way when their flags are set, guarded by a mechanical byte-count collapse guard against an accidental whole-file wipe. Purely additive — *except* when this cycle detects a commit reverting or materially changing something a prior cycle already wrote (cross-cycle revert/pivot detection): that stale entry is struck or superseded, never left stale beside a contradicting new one. A genuine `git revert` is resolved deterministically against a per-branch patch-id-keyed provenance store (`.temp/release-provenance-<branch>.json`, recording which commit's diff-content wrote which exact bullet — keyed on `git patch-id --stable` rather than raw commit sha, so a reword, rebase, or cherry-pick of the original commit since it was recorded doesn't defeat the lookup); a pivot (no literal revert commit) still relies on grep-narrowed semantic judgment. **After merge**, a post-merge re-validation pass re-runs Truth check, Identify highlights re-ranking, Validate migration docs, and Validate docs against the final merged content (not just this cycle's incremental diff) — catches prior-cycle content gone stale from this cycle's changes without a clean detected revert/pivot (e.g. a spotlight built on a commit a later cycle reverts gets replaced, not left beside the new set). No marker found (first use, or history rewritten by rebase/force-push) → falls back to the default `$LAST_TAG..HEAD` range and a full overwrite — identical to plain `notes` — establishing the baseline for the next `--append` run. Every successful `notes`-mode write refreshes the marker to current `HEAD`.

**Output location:** `releases/<version>/` for `prepare` artifacts (the project `CHANGELOG.md` is updated at its discovered path and linked from the release directory); `.temp/` for individual modes and demo on non-release branches.

______________________________________________________________________

### /oss:setup

**Purpose**: Deliver this plugin's `rules/*.md` into Claude's user-level rule namespace. Maintenance command, not part of any OSS workflow.

**When to use**: after installing oss on a new machine, or after upgrading it. `make sync-claude` runs it automatically for every installed managed plugin that ships a setup skill, so a normal sync needs no manual step.

**Invocation**:

```text
/oss:setup            # interactive — asks before replacing anything it does not own
/oss:setup --approve  # non-interactive — used by make sync-claude
```

Each rule installs as a symlink at `~/.claude/rules/oss-<source-name>.md`. The `oss-` prefix keeps the flat rule namespace collision-free — four plugins ship a `rules/quality-gates.md`. A filename prefix does not change how Claude loads a rule or how its `paths:` frontmatter matches.

Only links this plugin provably owns are replaced or removed: the existing target must resolve under the current plugin root or under the same install-cache lineage. A real file, a link into another marketplace, a source checkout, or a dotfiles tree is reported as a conflict and left alone unless you approve replacing it.

**Uninstall leaves rule links behind**: Claude Code runs no cleanup hook on uninstall, so `~/.claude/rules/oss-*.md` survives both `claude plugin uninstall` and `make clear-all`. Delete those symlinks by hand — once the plugin cache version is gone they dangle.

______________________________________________________________________

## 🤖 Agents reference

### gh-scraper

**Role:** Raw GitHub data fetcher for `/oss:analyse vitality`. Fetches all GitHub API data (REST + GraphQL) across two parallel groups, writes raw JSONL consumed by oss:repo-warden axis scorers. Internal — spawned by oss:analyse vitality Step 1 only.

**Model:** Sonnet (focused data collection)

**What gh-scraper does:**

- Runs all GitHub API fetch calls parallel (Group 1), then Group 2 (README, CONTRIBUTING, workflow files, branch protection)
- Retries contributor stats (202 computing) up to 6× before writing partial record
- Writes `raw-data-{owner}-{repo}-{date}.jsonl` for reproducibility + scorer consumption

**What gh-scraper does NOT do:**

- Axis scoring → oss:repo-warden owns all axis scoring
- Report generation, terminal output, adversarial review → oss:analyse vitality Steps 4–7 own those
- Direct user invocation — always spawned by vitality skill

______________________________________________________________________

### repo-warden

**Role:** Axis scorer for `/oss:analyse vitality`. Reads pre-fetched raw JSONL from oss:gh-scraper, scores assigned group of vitality axes per vitality-scoring.md rubric; writes partial scores JSON for assembly. Spawned 3× parallel by oss:analyse vitality Step 2 — not for direct user invocation.

**Model:** Sonnet (focused computation)

**What repo-warden does:**

- Scores assigned axis group (A: Axes 1,2,5,6 / B: Axes 4,7,8 / C: Axes 3,9) from DATA_FILE
- Runs Axis 3 multi-pass confidence logic; applies fallback from commits_50 when contributor stats unavailable
- Writes `partial-{group}-{owner}-{repo}-{ts}.json` consumed by vitality Step 3 assembly

**What repo-warden does NOT do:**

- Raw data fetching → oss:gh-scraper owns all GitHub API calls
- Report generation, terminal output, adversarial review → oss:analyse vitality Steps 4–7 own those
- Direct user invocation — always spawned by vitality skill

______________________________________________________________________

### oss:shepherd

**Role:** Public voice of project. Owns all external-facing communication — PR replies, issue responses, release notes, changelog entries, migration guides. Never writes implementation code.

**Model:** opusplan

**When to use shepherd directly:**

```bash
use shepherd to draft a response for issue #88, citing the contributing guide
use shepherd to review this changelog entry for tone before I post it
use shepherd to write a migration guide for the v3.0 breaking changes
```

**What shepherd does:**

- **Issue triage:** Classifies every issue into one of seven archetypes (bug confirmed, feature request, question/support, duplicate, stale, out of scope, breaking change), drafts response fitting each
- **Close-scenario replies:** Seven close archetypes from shepherd playbook — fixed in release, fixed on `develop`, superseded by architecture change, external/wrong repo, self-resolved/stale, keep open + relabel, superseded PR
- **PR review response:** Two-part format — leads with genuinely good, then specific actionable asks with line references; never adversarial
- **SemVer validation:** Reads actual diff, enforces correct bump type before any release proceeds
- **Release pipeline:** Writes release notes, changelog entries, migration guides in consistent project voice
- **Deprecation lifecycle:** Works with `pyDeprecate`; tracks deprecated APIs, writes migration guides, enforces deprecation → warning → removal timeline

**What shepherd does NOT do:**

- Inline docstrings or API reference docs → use `foundry:doc-scribe`
- CI pipeline configuration or GitHub Actions YAML structure for publish/release workflows → use `oss:cicd-steward`
- Implementation code of any kind

**Voice principles:**

- Leads with what's good
- Treats contributors as partners, never supplicants
- Cites specific conventions (contributing guide, coding style) when asking for changes
- Never adversarial, never dismissive of effort

______________________________________________________________________

### oss:cicd-steward

**Role:** GitHub Actions health specialist. Owns CI configuration quality: workflow topology, runner strategy, caching, branch protections, flaky test detection.

**Model:** Sonnet (fast iteration on workflow YAML)

**When to use cicd-steward directly:**

```bash
use cicd-steward to reduce the build time in .github/workflows/ci.yml
use cicd-steward to diagnose the failing test matrix on PR #72
use cicd-steward to add SHA pinning to all actions in the workflow
```

**What cicd-steward does:**

- Diagnoses CI failures by failure type (linting, type errors, test failures, import errors, timeouts, OOM)
- Audits GitHub Actions workflow files for antipatterns (unpinned actions, missing concurrency groups, broken caching, wrong parallelism)
- Optimises build time toward targets: unit tests < 5 min, full CI < 15 min
- Enforces cache hit rate > 80% using `astral-sh/setup-uv` with `uv.lock`-keyed caching
- Detects + quarantines flaky tests (target: 0% flakiness)
- Configures test matrices, reusable workflows, nightly upstream CI, performance regression benchmarks

**SHA pinning enforcement** (cicd-steward flags these as primary findings):

| Severity  | Pattern              | Example                                                           |
| --------- | -------------------- | ----------------------------------------------------------------- |
| Critical  | Branch/named refs    | `uses: actions/checkout@main`                                     |
| High      | Mutable version tags | `uses: actions/checkout@v4`                                       |
| Compliant | Full 40-char SHA     | `uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683` |

Short SHAs (fewer than 40 hex chars) treated as unpinned — can collide, not cryptographically safe.

**What cicd-steward does NOT do:**

- ruff/mypy rule selection or `.pre-commit-config.yaml` authoring → use `foundry:linting-expert` (IS for CI workflow steps that invoke pre-commit, e.g. `pre-commit/action@SHA`)
- PyPI release management, release notes, CHANGELOG entries, contributor communication → use `oss:shepherd`
- PyPI project registration, OIDC trusted publisher setup on pypi.org dashboard, GitHub environment configuration → use `oss:shepherd`

**Health targets:**

| Metric          | Target                                         |
| --------------- | ---------------------------------------------- |
| Main branch     | Green 100% of the time                         |
| Unit test suite | < 5 minutes                                    |
| Full CI         | < 15 minutes                                   |
| Cache hit rate  | > 80%                                          |
| Flaky tests     | 0% — any flaky test is quarantined immediately |

______________________________________________________________________

## ⚙️ Configuration

`oss` needs no required configuration — reads project structure automatically.

**GitHub authentication:** Skills use `gh` CLI. Run `gh auth login` once if not already.

**Permission manifests:** `.claude-plugin/permissions-allow.json` lists the tool calls the skills expect to be pre-approved. `.claude-plugin/permissions-deny.json` is its counterpart — the operations that must stay denied no matter how broad the allow list becomes: destructive shell and git commands (`rm -rf`, `sudo`, `ssh`, `chmod 777`, branch and tag deletion, force-push, `claude --dangerously-skip-permissions`), every public-GitHub write (`gh issue`/`pr`/`release`/`gist` create, edit, merge, delete, and `gh api` with `POST`, `PATCH`, `PUT` or `DELETE`), and the `curl` write verbs the broad `Bash(curl:*)` allowance would otherwise reach. Both files are merged into `~/.claude/settings.json` by `/oss:setup` (Step 5) — additive and idempotent, nothing is ever removed. Deny entries are prefix matches, so they stop the documented command forms rather than every possible flag ordering.

**Optional plugin integrations** detected automatically at runtime. Install any optional plugin from [Install](#oss-install) — skills use them next invocation, no config changes.

**Hooks** register automatically from `hooks/hooks.json` when the plugin is enabled — no `settings.json` edits needed:

| Hook                        | Event                                                                                                     | Behaviour                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `agent-router.js`           | `PreToolUse` (`Agent`)                                                                                    | Reroutes `Agent()` calls when the requested agent is not installed: exact match → semantic match → `general-purpose`.                                                                                                                                                                                                                                                                                                                                  |
| `sentinel-read-allow.js`    | (decision module, no longer registered as a hook of its own; called by `allow-dispatch.js` in rank order) | Auto-allows the pre-canned TMPDIR sentinel-read and `$(date +FMT)` idioms inside read-only commands, so skill bash blocks stop raising "Contains expansion" prompts. Everything else falls through to normal checks. Whole-line `# …` comments inside a block are skipped rather than blocking it, and `..` is matched as a path component so ellipsis and version ranges are not mistaken for traversal.                                              |
| `blueprint-allow.js`        | (decision module, no longer registered as a hook of its own; called by `allow-dispatch.js` in rank order) | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); normalizes the command, hashes it, and exact-matches it against this plugin's committed `blueprint-manifest.json`, so verbatim bash shipped in its own skills runs without a prompt. Any deviation from the blueprint text misses and falls through to a normal prompt.                                                                                        |
| `allow-dispatch.js`         | `PreToolUse` (`Bash`)                                                                                     | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); the only registered Bash auto-allow hook. Calls `blueprint-allow.js` then `sentinel-read-allow.js` as libraries, emits the first allow, and appends one audit record naming the effective verdict and both modules' own verdicts. Exits 0 on every path: an allow-only hook that crashes grants nothing and the call falls back to normal permission handling. |
| `audit-close.js`            | `PostToolUse`, `PostToolUseFailure` (`Bash`), `SessionStart`, `SessionEnd`                                | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); appends one record observing that a Bash call completed, and one per session boundary. Writes nothing to stdout on any event. Every installed plugin writes its own rows with nothing coordinating them.                                                                                                                                                       |
| `lib/audit-log.js`          | (shared module, not a hook)                                                                               | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); the shared record writer behind the two hooks above. Append-only — no lock, marker, state directory or deletion path — and never throws, so a logging failure cannot change a permission decision. `RIG_AUDIT=0` disables writing only.                                                                                                                        |
| `write-guard.js`            | `PreToolUse` (`Edit`, `Write`, `NotebookEdit`)                                                            | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); grants nothing — forces a confirmation on writes to `.github/**`, `CLAUDE.md`/`AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml`, `CHANGELOG.md`, dependency lockfiles and `pyproject.toml`. Source and tests are deliberately unprotected; everything else is silent passthrough.                                                               |
| `enforce-review-header.js`  | `PreToolUse` (`AskUserQuestion`)                                                                          | Denies `/oss:review`'s follow-up question until the consolidated `review-report.md` exists, so the report header always reaches the terminal first. Silent unless a review run is in flight. Once the report exists, additionally nudges (never blocks) via `additionalContext` when the reply never rendered the header as a table — see `report-header-table.js`.                                                                                    |
| `enforce-analyse-header.js` | `PreToolUse` (`AskUserQuestion`)                                                                          | Same gate for `/oss:analyse`: denies the Step 6a follow-up question until the running mode (thread, vitality or ecosystem) has written its report under `.reports/analyse/`. Silent unless an analyse run is in flight. Same table-format nudge as above once the report exists.                                                                                                                                                                       |
| `report-header-table.js`    | (shared module, not a hook)                                                                               | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); reads the session transcript to check whether the assistant's own reply, since the last human turn, rendered the report's `---` header as a `\| Field \| Value \|` table (or the documented `·`-fallback line) — catches the PR #1303 incident (raw YAML fields printed instead of a table) that the file-existence gate alone could not.                      |

**Cache location:** `.cache/gh/` at project root. Cached responses: 30-day TTL. Force fresh fetch: delete relevant cache file or entire `.cache/gh/` directory.

**Artifact directories** created by `oss` skills:

| Directory             | Created by             | Contents                                         |
| --------------------- | ---------------------- | ------------------------------------------------ |
| `.reports/analyse/`   | `/oss:analyse`         | Thread, vitality, ecosystem reports              |
| `.temp/review/`       | `/oss:review`          | Per-agent handover files (intermediate, per-run) |
| `.reports/resolve/`   | `/oss:resolve`         | Resolve run outputs                              |
| `.temp/`              | All skills             | Long-form output files                           |
| `.cache/gh/`          | `/oss:analyse`         | GitHub API response cache                        |
| `releases/<version>/` | `/oss:release prepare` | Release artefacts                                |

Artifact directories are gitignored; GitHub API cache entries use the 30-day TTL described above.

______________________________________________________________________

<a id="bin-helper-inventory"></a>

<details>

<summary><strong>🧰 Bin helper inventory (33 shipped deterministic helpers)</strong></summary>

These helpers are installed workflow support and maintainer surfaces, not additional slash-command skills. The skills own the orchestration; the helpers handle bounded parsing, evidence collection, path resolution, scoring, and artifact preparation.

#### Analysis, signals, and structural context

| Helper                           | Purpose                                                               |
| -------------------------------- | --------------------------------------------------------------------- |
| `assemble_vitality_scores.py`    | Merge three vitality-axis partials into one health score.             |
| `build_triage_batch.py`          | Build a codemap query batch from triaged identifiers.                 |
| `check_agent.py`                 | Probe whether a plugin agent is installed.                            |
| `check_oss_pr_signals.py`        | Collect read-only OSS signals from a pull-request diff.               |
| `classify_breaking.py`           | Label changed public symbols as Breaking or internal.                 |
| `classify_pr_scope.py`           | Classify a pull request as CHORE, FIX, REFACTOR, FEATURE, or MIXED.   |
| `codemap_cache.py`               | Materialize review-to-resolve codemap pre-flight cache artifacts.     |
| `detect_codemap.py`              | Detect codemap availability, index presence, and currency.            |
| `detect_thread_type.py`          | Detect GitHub thread type and report drift.                           |
| `extract_changed_symbols.py`     | Extract changed public Python symbols from a diff.                    |
| `extract_diff_impact_qnames.py`  | Extract qualified names from codemap diff-impact JSON.                |
| `extract_vitality_vars.py`       | Emit shell assignments from vitality-score JSON.                      |
| `fetch_gh_data_group1.py`        | Fetch independent GitHub datasets for vitality scoring.               |
| `fetch_gh_data_group2.py`        | Fetch dependent repository and workflow content for vitality scoring. |
| `resolve_centrality.py`          | Convert codemap centrality output into a worktree resolver map.       |
| `search_downstream_consumers.py` | Find GitHub repositories importing changed symbols.                   |

#### Review, resolve, and argument helpers

| Helper                       | Purpose                                                            |
| ---------------------------- | ------------------------------------------------------------------ |
| `commit_action_item.py`      | Manage the commit sentinel around one resolve action-item commit.  |
| `commit_all_items.py`        | Create a bulk commit summarizing resolved review items.            |
| `commit_lint_fixes.py`       | Stage tracked lint changes and create the lint-fix commit.         |
| `compute_commit_sentinel.py` | Print the current repository and branch commit-sentinel path.      |
| `heal_git_artifacts.py`      | Reclaim stale resolve locks and orphaned git worktrees.            |
| `merge_specialist_batch.py`  | Cherry-pick specialist worktree commits in priority order.         |
| `parse-resolve-args.py`      | Parse `/oss:resolve` arguments into shell assignments.             |
| `parse-skill-flags.py`       | Parse shared skill flags into shell assignments.                   |
| `parse_audit_json.py`        | Summarize `pip-audit` JSON as dependency and vulnerability counts. |
| `resolve_preflight.py`       | Verify tools, authentication, and remote state before resolve.     |
| `resolve_shared_path.py`     | Resolve the plugin's shared directory portably.                    |

#### Release, installation, and path helpers

| Helper                       | Purpose                                                    |
| ---------------------------- | ---------------------------------------------------------- |
| `extract_contributors.py`    | List unique non-bot contributors in a Git range.           |
| `get_plugin_install_path.py` | Resolve the active plugin path from Claude's registry.     |
| `release_append_marker.py`   | Persist and resolve the release `--append` baseline.       |
| `release_setup.py`           | Resolve shared setup values for release modes.             |
| `run_audit_checks.py`        | Gather raw readiness evidence for release audit.           |
| `setup_release_dir.py`       | Create a release directory and protect existing artifacts. |
| `sync_rules.py`              | Install namespaced rule symlinks into `~/.claude/rules/`.  |
| `verify_blueprint_audit.py`  | Verify and prune the auto-allow audit log.                 |

</details>

#### Auto-allow audit log

`allow-dispatch.js` and `audit-close.js` append one JSON line each per Bash call to `~/.claude/logs/audit/`: one file per session (`s-<key>.jsonl`, the key being a hash of the session id) plus a shared `_no-session.jsonl` for calls that arrive without one. A record names the deciding plugin and hook, the session and tool-call ids, the working directory, the effective decision with its lane and provenance, and both decision modules' own verdicts. Command text is never written — only a digest, and for a provenance allow the manifest `src` it matched. Note the limit honestly: `task-log.js` already writes the first 200 characters of every Bash command into `timings.jsonl` under the same `tool_use_id`, so the guarantee is about this file rather than about the log directory.

With several plugins installed each writes its own rows and nothing coordinates them, so one completed call yields one row per plugin at each end. That is corroboration, not duplication.

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" verify
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" prune --older-than 30 --dry-run
```

`verify` exits 1 only when a record's own hash fails to verify; torn framing from concurrent appends and every classification are warnings. `record_hash` detects a record that changed after it was written — it is not tamper-evidence, because the same user owns the log, the hooks and the checker. Read the classifications as observations rather than proof: a call with a decision row and no completion row is reported as `completion-unobserved` when the session later ended and `in-flight-or-hard-kill` when it did not, and neither distinguishes a refusal at the prompt from a deny rule elsewhere or a closer that died. Equally, that a tool ran never establishes that a human approved it. `prune` is the only component that deletes a log file, is never run by a hook, and never prunes `_no-session.jsonl` by age — a growing one means the host stopped sending a session id. Nothing schedules `prune`. `RIG_AUDIT=0` disables audit writing without changing any permission decision.

Records follow the twelve mandatory fields of ["Agent Audit Trail: A Standard Logging Format for Autonomous AI Systems"](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/), an active individual-submission Internet-Draft that is not endorsed by the IETF and has no standing in its standards process. Deliberate deviations: `outcome` adds `pending`, because a decision that has not executed yet has no outcome in the draft's vocabulary; `trust_level` is `plugin`/`unknown` rather than the draft's `L0`–`L4`, because what is observed is which component proposed an allow, not an authentication level; `agent_id` is `<plugin>/<hook>` rather than a URI; `session_id` is the host's id verbatim rather than a UUID; and `parent_record_id` and `prev_hash` are always null, because this log is not chained — lineage is derived on read instead.

<a id="troubleshooting"></a>

<details>

<summary>

## 🔍 Troubleshooting

</summary>

**`/oss:review` skips Tier 2 agents**

Review dimensions are scope-selected and the default fan-out is capped at four. Install `foundry` for specialized agents; otherwise the selected work uses `general-purpose` fallbacks. Install `bridge@borda-ai-rig` only if you want optional Codex co-review.

**A question is blocked with "oss:review report gate"**

`enforce-review-header.js` denied an `AskUserQuestion` call because `.reports/review/<timestamp>/review-report.md` does not exist — the review reached agent launch but never consolidated its findings into a report. Finish the consolidation step and print the report `---` header; the question then goes through. The gate deactivates two hours after a run starts, so an aborted review never blocks later questions permanently. Once the report exists, the hook also checks (via `report-header-table.js`) whether the printed reply actually rendered the header as a table — a missing table never blocks the question, but rides along as an `additionalContext` reminder naming Step 5b.

**A question is blocked with "oss:analyse report gate"**

`enforce-analyse-header.js` denied an `AskUserQuestion` call because the report the running mode announced — `.reports/analyse/thread/…`, `.reports/analyse/vitality/…` or `.reports/analyse/ecosystem/…` — is missing or empty, so the follow-up question would offer next steps for an analysis that was never saved. Write the report, print its `---` header, then re-ask. Same two-hour deactivation as the review gate; the window is measured from the point the mode announces its report path, not from the start of the run. Same table-format nudge as `enforce-review-header.js` once the report exists.

**`/oss:review` uses general-purpose agents instead of specialist agents**

`foundry` plugin not installed or not detected. Install: `claude plugin install foundry@borda-ai-rig`. All skills degrade gracefully to general-purpose agents when `foundry` absent.

**`/oss:resolve` pauses mid-run asking for confirmation**

More than 10 selected items found across sources. Intentional — resolve asks whether to batch or continue with the slower larger dispatch; a single dispatch never exceeds 20 items.

**`/oss:resolve` aborts with "too many conflicted files"**

More than 20 files have semantic conflicts. Resolve aborts rather than guessing intent at scale. Review conflict list in output, resolve most complex manually, re-run resolve on remainder.

**`/oss:release` reports a version or release-state problem**

The readiness audit found a version-consistency or release-state gap. Review the audit table, update the project manifest/changelog as appropriate, and re-run `/oss:release audit [version]`; this skill does not edit package versions for you.

**`/oss:analyse` returns stale data**

Cached GitHub API responses served from `.cache/gh/`. Delete cache file for specific thread number or clear `.cache/gh/` entirely for fresh fetch.

**Skills not found after install**

Run `claude plugin install oss@borda-ai-rig` again, then `/reload-plugins` in Claude Code.

______________________________________________________________________

</details>

<a id="contributing--feedback"></a>

## 🙏 Contributing / feedback

`oss` = part of Borda-AI-Rig plugin suite. Contribute or report issues:

- **Bugs and feature requests:** Open issue in Borda-AI-Rig repository
- **Plugin authoring rules:** See `plugins/CLAUDE.md` — file layout, naming conventions, cross-plugin references, README sync requirements, versioning policy
- **Voice and tone:** All contributor-facing text follows same principles as `oss:shepherd` — welcoming, specific, treats contributors as partners

Editing `oss` skills or agents → update this README before commit. Rule in `plugins/CLAUDE.md`: changed trigger, scope, NOT-for, or hook behaviour → update README description. Added/removed agent/skill → update table. Unsynced change = incomplete.

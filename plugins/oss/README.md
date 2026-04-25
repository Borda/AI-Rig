# 🌱 oss — Claude Code Plugin

OSS workflow plugin for Python/ML open-source projects. Two specialist agents and four slash-command skills covering issue analysis, parallel code review, PR resolution, and SemVer-disciplined releases.

> Works standalone — `foundry` is not required. Without it, agent dispatches fall back to `general-purpose` with role descriptions (lower quality). Installing `foundry` unlocks specialized agents (`foundry:sw-engineer`, `foundry:qa-specialist`, etc.) and is strongly recommended for production use.

______________________________________________________________________

<details>
<summary><strong>📋 Contents</strong></summary>

- [What is oss?](#what-is-oss)
- [Why oss?](#why-oss)
- [Install](#install)
- [Quick start](#quick-start)
- [Skills reference](#skills-reference)
  - [/oss:analyse](#ossanalyse)
  - [/oss:review](#ossreview)
  - [/oss:resolve](#ossresolve)
  - [/oss:release](#ossrelease)
- [Agents reference](#agents-reference)
  - [oss:shepherd](#ossshepherd)
  - [oss:ci-guardian](#ossci-guardian)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Contributing / feedback](#contributing--feedback)

</details>

______________________________________________________________________

## 🤔 What is oss?

`oss` is a Claude Code plugin for maintainers of Python and ML open-source projects. It gives you four slash-command skills — analyse, review, resolve, release — and two specialist agents that own contributor communication and CI health. Together they cover the recurring, expensive parts of the maintainer loop: triaging GitHub threads, reviewing PRs with multiple expert perspectives, closing review feedback in one pass, and cutting releases with correct SemVer and complete changelogs.

______________________________________________________________________

## 🎯 Why oss?

Maintaining an open-source project means juggling three competing demands: reviewing code carefully enough to catch regressions, responding to contributors quickly enough that they stay engaged, and shipping releases confidently enough that users upgrade. Each of these is a context-switch tax.

`oss` removes that tax. Here is what it actually does for you:

**You review a PR in minutes, not hours.** `/oss:review` runs a Codex pre-pass in about 60 seconds. Trivial PRs close right there. For anything substantive, six specialist agents fan out in parallel — architecture, tests, performance, docs, linting, security — and hand you a ranked, consolidated report. You get expert-level coverage without reading every line yourself.

**Contributors get a real response, fast.** The `--reply` flag drafts a welcoming comment in your project's voice, citing your specific conventions. You spend 30 seconds reviewing it instead of 10 minutes writing from scratch. Contributors feel heard; they stay engaged.

**Review feedback gets applied completely.** `/oss:resolve` closes the gap between "reviewer said X" and "X is in the code." It reads live PR comments, a saved review report, or both, deduplicates across sources, resolves conflicts semantically (not by picking a side mechanically), and implements everything in batches with `[resolve #N]` tags so you can trace each fix.

**Releases go out correctly.** `oss:shepherd` enforces SemVer before any tag lands. It writes the changelog with deprecation tracking, generates migration guides for breaking changes, and runs a readiness audit. No accidental major bumps. No forgotten changelog entries.

**Triage is fast and structured.** `/oss:analyse health` gives you a repo overview with duplicate issue clustering and stale PR detection every morning. Drilling into a specific thread gives you a structured summary you can act on immediately.

______________________________________________________________________

## 📦 Install

```bash
# Run from the directory that CONTAINS your Borda-AI-Rig clone
claude plugin marketplace add ./Borda-AI-Rig
claude plugin install oss@borda-ai-rig
```

Install the full suite for best results:

```bash
claude plugin install foundry@borda-ai-rig   # base agents — strongly recommended
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
```

**Optional integrations** (unlock additional capabilities inside `oss` skills):

| Plugin    | What it unlocks                                                                                                                                             |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `foundry` | Specialized review agents (sw-engineer, qa-specialist, perf-optimizer, doc-scribe, solution-architect, linting-expert) instead of general-purpose fallbacks |
| `codex`   | Action item implementation in `/oss:resolve` Step 8; Tier 1 pre-pass in `/oss:review`                                                                       |
| `codemap` | Reverse-dependency count (`rdep_count`) in `/oss:review` risk assessment                                                                                    |

All `oss` skills degrade gracefully when optional plugins are absent — you get reduced capability, not broken commands.

> **Note:** Skills are always invoked with the `oss:` prefix: `/oss:analyse`, `/oss:review`, `/oss:resolve`, `/oss:release`.

<details>
<summary><strong>Upgrade</strong></summary>

```bash
cd Borda-AI-Rig && git pull
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
/oss:analyse health

# Review top PR and draft a contributor-facing response
/oss:review 55 --reply

# Apply all review feedback in one pass
/oss:resolve 55 pr report

# Cut a release
/oss:release prepare v2.1.0
```

______________________________________________________________________

## 🔧 Skills reference

### /oss:analyse

Analyse GitHub threads and repo health. Accepts an issue or PR number, the keyword `health`, the keyword `ecosystem`, or a path to a saved report file.

**Purpose:** Give you a structured, actionable summary of any GitHub thread or a broad view of your repo's open work. Saves you from reading every comment yourself.

**Invocation:**

```text
/oss:analyse 123              # issue, PR, or discussion by number
/oss:analyse health           # repo overview: open issues, stale PRs, duplicate clustering
/oss:analyse ecosystem        # dependency health, upstream compatibility
/oss:analyse path/to/report.md  # re-analyse a saved report
```

**Flags:**

| Flag      | Effect                                                                                                   |
| --------- | -------------------------------------------------------------------------------------------------------- |
| `--reply` | Draft a contributor-facing response after analysis (routed through `oss:shepherd` for voice consistency) |

**What it does:**

For a thread number, `analyse` fetches the issue or PR, reads all comments, classifies the thread type (bug report, feature request, question, duplicate, stale), and produces a structured summary: what was asked, what the current state is, what action is needed from you, and — with `--reply` — a draft response.

For `health`, it pulls open issues and PRs, clusters duplicates, flags threads stale beyond your project's threshold, and gives you a prioritised triage list.

**Output locations:**

- Thread analysis: `.reports/analyse/thread/`
- Health report: `.reports/analyse/health/`
- Ecosystem report: `.reports/analyse/ecosystem/`

GitHub API responses are cached in `.cache/gh/` by number and date (30-day TTL) — repeated calls on the same thread are fast.

______________________________________________________________________

### /oss:review

Tiered parallel review of a GitHub PR. Input is always a PR number.

**Purpose:** Give you expert-level review coverage across architecture, tests, performance, docs, linting, and security — without reading every line yourself. Produces a ranked findings report. Optionally drafts a welcoming contributor comment.

**Invocation:**

```text
/oss:review 55                # full tiered review — saves findings report
/oss:review 55 --reply        # review + draft contributor-facing comment
```

> To review local files or the current git diff without a PR, use `/develop:review` from the `develop` plugin.

**How the pipeline works:**

```text
Tier 0  git diff --stat
        Mechanical gate — skips trivial diffs (whitespace-only, docs-only changes)

Tier 1  Codex pre-pass (~60 seconds)
        Independent diff review; surfaces obvious issues first
        If blocking issue found → report immediately, skip Tier 2

Tier 2  Six parallel specialist agents (requires foundry plugin)
        foundry:sw-engineer        — correctness, design, API contracts
        foundry:qa-specialist      — test coverage, edge cases, regression risk
        foundry:perf-optimizer     — hot paths, memory, algorithmic complexity
        foundry:doc-scribe         — docstrings, README accuracy, examples
        foundry:solution-architect — architecture fit, dependency impact
        foundry:linting-expert     — style, type annotations, ruff/mypy

        Agent skip rules:
          FIX commits   → skips perf-optimizer and linting-expert
          REFACTOR      → skips linting-expert

        codemap integration: rdep_count > 20 flags as high-risk change

        Consolidation: foundry:sw-engineer merges all agent findings into ranked report

        --reply: oss:shepherd writes contributor-facing comment from consolidated report
```

Without `foundry`, Tier 2 falls back to general-purpose agents with role descriptions — still functional, lower quality.

**Output locations:**

- Per-agent findings: `.reports/review/<timestamp>/`
- Consolidated report: `.temp/output-review-<branch>-<date>.md`
- Reply draft (with `--reply`): `.temp/output-reply-<PR#>-<date>.md`

**Flags:**

| Flag      | Effect                                                                                                                             |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `--reply` | After consolidation, `oss:shepherd` drafts a welcoming two-part PR comment (positive framing first, then specific actionable asks) |

______________________________________________________________________

### /oss:resolve

Apply review findings to the codebase. Reads from live PR comments, a saved review report, or both — deduplicates, resolves conflicts, and implements fixes.

**Purpose:** Close the gap between "reviewer said X" and "X is in the code." One command takes you from open findings to committed fixes.

**Invocation:**

```text
/oss:resolve 55 pr             # apply fixes from live GitHub PR comments
/oss:resolve 55 report         # apply fixes from the saved /oss:review report
/oss:resolve 55 pr report      # both sources — Codex deduplicates across inputs
/oss:resolve                   # review-handoff mode — picks up from the last /oss:review run
```

**Source modes:**

| Mode        | Source                            | When to use                                                        |
| ----------- | --------------------------------- | ------------------------------------------------------------------ |
| `pr`        | Live GitHub PR comments           | Apply feedback posted directly on GitHub                           |
| `report`    | Saved `/oss:review` findings file | Apply findings from your last review run                           |
| `pr report` | Both, aggregated                  | Full close — deduplicates across both inputs                       |
| _(no args)_ | Review-handoff                    | Continues directly from the last `/oss:review` run in this session |

**How it works:**

Resolve runs in three phases:

1. **Intelligence gathering** — fetch and parse all sources; classify each finding as a requirement, suggestion, or conflict; deduplicate across sources
2. **Conflict resolution** — for merge conflicts, read intent from both sides; apply the semantically correct resolution (never mechanical "take ours" or "take theirs")
3. **Action item implementation** — apply fixes in batches of 10; each fix tagged `[resolve #N]` in the commit for traceability

**Guard rails:**

- More than 15 required items → pauses and asks you to confirm before continuing
- More than 20 conflicted files → aborts and reports; you review manually
- Core invariant: uses `git merge`, never `git rebase` — preserves history

Every resolve cycle closes with parallel `foundry:linting-expert` + `foundry:qa-specialist` passes before the final report.

**Output location:** `.reports/resolve/<timestamp>/`

______________________________________________________________________

### /oss:release

SemVer-disciplined release pipeline. Six modes covering every step from generating notes to auditing readiness.

**Purpose:** Cut a release correctly — right version bump, complete changelog, migration guides where needed, readiness verified before the tag goes out.

**Invocation:**

```text
/oss:release notes v1.2.0..HEAD     # generate release notes from a git range
/oss:release changelog v2.1.0       # write or update CHANGELOG.md entry
/oss:release summary v2.1.0         # short summary for GitHub release body
/oss:release migration v2.1.0       # generate migration guide for breaking changes
/oss:release prepare v2.1.0         # full pipeline: notes + changelog + migration + audit
/oss:release audit                  # readiness check before tagging
```

**Modes:**

| Mode        | What it produces                                                                            |
| ----------- | ------------------------------------------------------------------------------------------- |
| `notes`     | Human-readable release notes from git log in the specified range                            |
| `changelog` | CHANGELOG.md entry following Keep a Changelog format                                        |
| `summary`   | Short paragraph for the GitHub release body                                                 |
| `migration` | Step-by-step migration guide for breaking API changes                                       |
| `prepare`   | Runs notes + changelog + migration + audit in sequence; writes to `releases/<version>/`     |
| `audit`     | Readiness checklist: tests green, changelog present, version bumped, no uncommitted changes |

**SemVer enforcement:**

`oss:shepherd` validates the proposed version bump against the actual diff before anything is written. It refuses to proceed if:

- A `patch` bump is proposed but the diff contains breaking changes → should be `major`
- A `minor` bump is proposed but no new public API was added → should be `patch`
- Version string does not follow `MAJOR.MINOR.PATCH` format

**CHANGELOG section ordering** (strict, enforced):

```text
Added → Breaking Changes → Changed → Deprecated → Removed → Fixed
```

**Deprecation tracking:** Uses `pyDeprecate` for the deprecation lifecycle. Migration guides include a before/after table with argument mapping for all renamed or removed parameters.

**All public-facing text** (release notes, changelog entries, migration guides) passes through `oss:shepherd` for voice review before being written to disk.

**Output location:** `releases/<version>/` for `prepare` mode; `.temp/` for individual modes.

______________________________________________________________________

## 🤖 Agents reference

### oss:shepherd

**Role:** The public voice of your project. Shepherd owns all external-facing communication — PR replies, issue responses, release notes, changelog entries, and migration guides. It never writes implementation code.

**Model:** opusplan (plan-gated Opus — thinks before acting on high-stakes text)

**When to use shepherd directly:**

```bash
use shepherd to draft a response for issue #88, citing the contributing guide
use shepherd to review this changelog entry for tone before I post it
use shepherd to write a migration guide for the v3.0 breaking changes
```

**What shepherd does:**

- **Issue triage:** Classifies every issue into one of seven archetypes (bug confirmed, feature request, question/support, duplicate, stale, out of scope, breaking change) and drafts a response appropriate to each
- **Close-scenario replies:** Uses seven close archetypes from the shepherd playbook — fixed in a release, fixed on `develop`, superseded by architecture change, external/wrong repo, self-resolved/stale, keep open + relabel, and superseded PR
- **PR review response:** Two-part format — leads with what is genuinely good, then gives specific actionable asks with line references; never adversarial
- **SemVer validation:** Reads the actual diff and enforces correct bump type before any release proceeds
- **Release pipeline:** Writes release notes, changelog entries, and migration guides in consistent project voice
- **Deprecation lifecycle:** Works with `pyDeprecate`; tracks deprecated APIs, writes migration guides, enforces the deprecation → warning → removal timeline

**What shepherd does NOT do:**

- Inline docstrings or API reference docs → use `foundry:doc-scribe`
- CI pipeline configuration → use `oss:ci-guardian`
- Implementation code of any kind

**Voice principles:**

- Leads with what is good
- Treats contributors as partners, never supplicants
- Cites specific conventions (contributing guide, coding style) when asking for changes
- Never adversarial, never dismissive of effort

______________________________________________________________________

### oss:ci-guardian

**Role:** GitHub Actions health specialist. Owns CI configuration quality: workflow topology, runner strategy, caching, branch protections, and flaky test detection.

**Model:** Haiku (fast iteration on workflow YAML)

**When to use ci-guardian directly:**

```bash
use ci-guardian to reduce the build time in .github/workflows/ci.yml
use ci-guardian to diagnose the failing test matrix on PR #72
use ci-guardian to add SHA pinning to all actions in the workflow
```

**What ci-guardian does:**

- Diagnoses CI failures by failure type (linting, type errors, test failures, import errors, timeouts, OOM)
- Audits GitHub Actions workflow files for antipatterns (unpinned actions, missing concurrency groups, broken caching, wrong parallelism)
- Optimises build time toward targets: unit tests < 5 min, full CI < 15 min
- Enforces cache hit rate > 80% using `astral-sh/setup-uv` with `uv.lock`-keyed caching
- Detects and quarantines flaky tests (target: 0% flakiness)
- Configures test matrices, reusable workflows, nightly upstream CI, and performance regression benchmarks

**SHA pinning enforcement** (ci-guardian flags these as primary findings):

| Severity  | Pattern              | Example                                                           |
| --------- | -------------------- | ----------------------------------------------------------------- |
| Critical  | Branch/named refs    | `uses: actions/checkout@main`                                     |
| High      | Mutable version tags | `uses: actions/checkout@v4`                                       |
| Compliant | Full 40-char SHA     | `uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683` |

Short SHAs (fewer than 40 hex characters) are treated as unpinned — they can collide and are not cryptographically safe.

**What ci-guardian does NOT do:**

- ruff/mypy rule selection or pre-commit configuration → use `foundry:linting-expert`
- PyPI release workflow or Trusted Publishing setup → use `oss:shepherd`

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

`oss` has no required configuration — it reads your project structure automatically.

**GitHub authentication:** Skills use the `gh` CLI. Run `gh auth login` once if you have not already.

**Optional plugin integrations** are detected automatically at runtime. Install any of the optional plugins listed in [Install](#install) and the skills will use them on the next invocation — no config changes needed.

**Cache location:** `.cache/gh/` at project root. Cached responses have a 30-day TTL. To force a fresh fetch, delete the relevant cache file or the entire `.cache/gh/` directory.

**Artifact directories** created by `oss` skills:

| Directory             | Created by                    | Contents                                |
| --------------------- | ----------------------------- | --------------------------------------- |
| `.reports/analyse/`   | `/oss:analyse`                | Thread, health, ecosystem reports       |
| `.reports/review/`    | `/oss:review`                 | Per-agent findings, consolidated report |
| `.reports/resolve/`   | `/oss:resolve`                | Resolve run outputs                     |
| `.temp/`              | All skills                    | Long-form output files                  |
| `.cache/gh/`          | `/oss:analyse`, `/oss:review` | GitHub API response cache               |
| `releases/<version>/` | `/oss:release prepare`        | Release artefacts                       |

All artifact directories are gitignored (ephemeral, TTL-managed).

______________________________________________________________________

<a id="troubleshooting"></a>

<details>
<summary>

## 🔍 Troubleshooting

</summary>

## 🔍 Troubleshooting

**`/oss:review` skips Tier 2 agents**

Tier 2 only runs when Tier 1 (Codex pre-pass) does not surface a blocking issue on its own. If Tier 1 flags something blocking, you will see it in the report. Install the `codex` plugin to enable the pre-pass; without it, `/oss:review` goes directly to Tier 2.

**`/oss:review` uses general-purpose agents instead of specialist agents**

`foundry` plugin is not installed or not detected. Install it with `claude plugin install foundry@borda-ai-rig`. All skills degrade gracefully to general-purpose agents when `foundry` is absent.

**`/oss:resolve` pauses mid-run asking for confirmation**

More than 15 required items were found across the sources. This is intentional — resolve asks before applying a large batch. Confirm to continue, or review the item list and tell resolve which items to skip.

**`/oss:resolve` aborts with "too many conflicted files"**

More than 20 files have semantic conflicts. Resolve aborts rather than guessing at intent at scale. Review the conflict list in the output, resolve the most complex ones manually, then re-run resolve on the remainder.

**`/oss:release` refuses to proceed with a proposed version bump**

`oss:shepherd` validated the diff and found the bump type is incorrect. The output will tell you what bump type is justified by the actual changes. Adjust your version argument and re-run.

**`/oss:analyse` returns stale data**

Cached GitHub API responses are served from `.cache/gh/`. Delete the cache file for the specific thread number or clear `.cache/gh/` entirely for a fresh fetch.

**Skills not found after install**

Run `claude plugin install oss@borda-ai-rig` again from the directory containing your Borda-AI-Rig clone, then `/reload-plugins` in Claude Code.

______________________________________________________________________

</details>

<a id="contributing--feedback"></a>

## 🙏 Contributing / feedback

`oss` is part of the Borda-AI-Rig plugin suite. To contribute or report issues:

- **Bugs and feature requests:** Open an issue in the Borda-AI-Rig repository
- **Plugin authoring rules:** See `plugins/CLAUDE.md` — covers file layout, naming conventions, cross-plugin references, README sync requirements, and versioning policy
- **Voice and tone:** All contributor-facing text follows the same principles as `oss:shepherd` — welcoming, specific, treats contributors as partners

When editing `oss` skills or agents, update this README before the commit. The rule in `plugins/CLAUDE.md` is: changed trigger, scope, NOT-for, or hook behaviour → update the README description. Added or removed agent/skill → update the table. Unsynced change = incomplete.

**Re: Compress markdown to caveman format**

<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: agents

### Domain table

Problem domain by agent:

- `foundry:sw-engineer` → Python bugs: type errors, logic errors, anti-patterns, bare `except:`, mutable defaults
- `foundry:qa-specialist` → coverage gaps: uncovered edge cases, missing exception tests, Machine Learning (ML) non-determinism
- `foundry:linting-expert` → violations: ruff rules, mypy errors, annotation gaps
- `foundry:curator` → config issues: broken cross-refs, missing workflow blocks, wrong model, step gaps; handover compliance: malformed JSON envelopes; context discipline: spawn prompt bloat, AgentSpeak v2 violations
- `foundry:doc-scribe` → docs gaps: missing docstrings, missing Google style sections, broken examples
- `foundry:perf-optimizer` → perf issues: unnecessary loops, repeated computation, wrong dtype, missing vectorisation
- `oss:ci-guardian` → Continuous Integration (CI) issues: non-pinned action Secure Hash Algorithms (SHAs), missing cache, inefficient matrix
- `research:data-steward` → data issues: label leakage, split contamination, augmentation order bugs, API pagination truncation, dataset completeness, provenance gaps
- `research:scientist` → paper analysis: missed contributions, wrong method attribution
- `foundry:solution-architect` → design issues: leaky abstractions, circular dependencies, missing Architecture Decision Record (ADR), backward-compat violations without deprecation path
- `foundry:web-explorer` → content quality: broken or unverified Uniform Resource Locators (URLs), outdated docs, incomplete extraction from fetched pages
- `oss:shepherd` → Open Source Software (OSS) governance: incorrect Semantic Versioning (SemVer) decision, missing CHANGELOG entry, bad deprecation path, wrong release checklist item
- `foundry:challenger` → plan/architecture challenges: missed assumptions, missing edge cases, unjustified blocker classification, skipped refutation step
- `foundry:creator` → content quality: narrative arc gaps, audience-profile mismatches, voice inconsistency, missing story beats, out-of-scope format acceptance

All agents support `ceiling` difficulty tier. Ceiling patterns by domain: `foundry:sw-engineer` → adversarial (idiomatic-looking but subtly wrong), concurrency bugs; `foundry:qa-specialist` → incomplete detectability (coverage gaps only visible at runtime); `foundry:perf-optimizer` → deep cross-function control flow; `research:data-steward` → adversarial (split contamination disguised as correct preprocessing); `foundry:solution-architect` → deep dependency tracing. Agents where ceiling infeasible (e.g., `foundry:linting-expert` — violations always statically detectable): generators may substitute hard problem.

### Step 2: Spawn agent pipeline subagents

Mark "Calibrate agents" in_progress. Per agent in domain table, spawn one `general-purpose` pipeline subagent. Issue ALL spawns in **single response** — agents independent, run concurrently.

Each subagent gets pipeline template from `.claude/skills/calibrate/templates/pipeline-prompt.md` with substitutions:

- `<TARGET>` = agent name (e.g., `foundry:sw-engineer`)
- `<DOMAIN>` = domain string from table above for that agent
- `<N>` = 3 (fast) or 10 (full)
- `<TIMESTAMP>` = current run timestamp
- `<MODE>` = `fast` or `full`
- `<AB_MODE>` = `true` or `false` — whether to run A/B variant scoring against `general-purpose` baseline (see pipeline-prompt.md Phase 2b)

Run dir per agent: `.reports/calibrate/<TIMESTAMP>/<TARGET>/`

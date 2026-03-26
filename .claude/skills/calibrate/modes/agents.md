## Mode: agents

### Domain table

Problem domain by agent:

- `sw-engineer` → Python bugs: type errors, logic errors, anti-patterns, bare `except:`, mutable defaults
- `qa-specialist` → coverage gaps: uncovered edge cases, missing exception tests, Machine Learning (ML) non-determinism
- `linting-expert` → violations: ruff rules, mypy errors, annotation gaps
- `self-mentor` → config issues: broken cross-refs, missing workflow blocks, wrong model, step gaps; handover compliance: malformed JSON envelopes; context discipline: spawn prompt bloat, AgentSpeak v2 violations
- `doc-scribe` → docs gaps: missing docstrings, missing Google style sections, broken examples
- `perf-optimizer` → perf issues: unnecessary loops, repeated computation, wrong dtype, missing vectorisation
- `ci-guardian` → Continuous Integration (CI) issues: non-pinned action Secure Hash Algorithms (SHAs), missing cache, inefficient matrix
- `data-steward` → data issues: label leakage, split contamination, augmentation order bugs, API pagination truncation, dataset completeness, provenance gaps
- `ai-researcher` → paper analysis: missed contributions, wrong method attribution
- `solution-architect` → design issues: leaky abstractions, circular dependencies, missing Architecture Decision Record (ADR), backward-compat violations without deprecation path
- `web-explorer` → content quality: broken or unverified Uniform Resource Locators (URLs), outdated docs, incomplete extraction from fetched pages
- `oss-shepherd` → Open Source Software (OSS) governance: incorrect Semantic Versioning (SemVer) decision, missing CHANGELOG entry, bad deprecation path, wrong release checklist item

### Step 2: Spawn agent pipeline subagents

Mark "Calibrate agents" in_progress. For each agent in the domain table, spawn one `general-purpose` pipeline subagent. Issue ALL spawns in a **single response** — agents are independent and run concurrently.

Each subagent receives the pipeline template from `.claude/skills/calibrate/templates/pipeline-prompt.md` with these substitutions:

- `<TARGET>` = the agent name (e.g., `sw-engineer`)
- `<DOMAIN>` = the domain string from the table above for that agent
- `<N>` = 3 (fast) or 10 (full)
- `<TIMESTAMP>` = current run timestamp
- `<MODE>` = `fast` or `full`
- `<AB_MODE>` = `true` or `false` — whether to run A/B variant scoring against a `general-purpose` baseline (see pipeline-prompt.md Phase 2b)

Run dir per agent: `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/`

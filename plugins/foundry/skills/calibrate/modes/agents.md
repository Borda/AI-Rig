<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: agents

### Domain table

Problem domain by agent:

- `sw-engineer` → Python bugs: type errors, logic errors, anti-patterns, bare `except:`, mutable defaults
- `qa-specialist` → coverage gaps: uncovered edge cases, missing exception tests, Machine Learning (ML) non-determinism
- `linting-expert` → violations: ruff rules, mypy errors, annotation gaps
- `self-mentor` → config issues: broken cross-refs, missing workflow blocks, wrong model, step gaps; handover compliance: malformed JSON envelopes; context discipline: spawn prompt bloat, AgentSpeak v2 violations
- `doc-scribe` → docs gaps: missing docstrings, missing Google style sections, broken examples
- `perf-optimizer` → perf issues: unnecessary loops, repeated computation, wrong dtype, missing vectorisation
- `oss:ci-guardian` → Continuous Integration (CI) issues: non-pinned action Secure Hash Algorithms (SHAs), missing cache, inefficient matrix
- `research:data-steward` → data issues: label leakage, split contamination, augmentation order bugs, API pagination truncation, dataset completeness, provenance gaps
- `research:ai-researcher` → paper analysis: missed contributions, wrong method attribution
- `solution-architect` → design issues: leaky abstractions, circular dependencies, missing Architecture Decision Record (ADR), backward-compat violations without deprecation path
- `web-explorer` → content quality: broken or unverified Uniform Resource Locators (URLs), outdated docs, incomplete extraction from fetched pages
- `oss:oss-shepherd` → Open Source Software (OSS) governance: incorrect Semantic Versioning (SemVer) decision, missing CHANGELOG entry, bad deprecation path, wrong release checklist item

All agents support the `ceiling` difficulty tier. Ceiling patterns particularly applicable by domain: `sw-engineer` → adversarial (idiomatic-looking but subtly wrong code), concurrency bugs; `qa-specialist` → incomplete detectability (coverage gaps only visible at runtime); `perf-optimizer` → deep cross-function control flow; `research:data-steward` → adversarial (split contamination disguised as correct preprocessing); `solution-architect` → deep dependency tracing. For agents where ceiling is infeasible (e.g., `linting-expert` — violations are always statically detectable), generators may substitute a hard problem.

### Step 2: Spawn agent pipeline subagents

Mark "Calibrate agents" in_progress. For each agent in the domain table, spawn one `general-purpose` pipeline subagent. Issue ALL spawns in a **single response** — agents are independent and run concurrently.

Each subagent receives the pipeline template from `.claude/skills/calibrate/templates/pipeline-prompt.md` with these substitutions:

- `<TARGET>` = the agent name (e.g., `sw-engineer`)
- `<DOMAIN>` = the domain string from the table above for that agent
- `<N>` = 3 (fast) or 10 (full)
- `<TIMESTAMP>` = current run timestamp
- `<MODE>` = `fast` or `full`
- `<AB_MODE>` = `true` or `false` — whether to run A/B variant scoring against a `general-purpose` baseline (see pipeline-prompt.md Phase 2b)

Run dir per agent: `.reports/calibrate/<TIMESTAMP>/<TARGET>/`

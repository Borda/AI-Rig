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
- `oss:cicd-steward` → Continuous Integration (CI) issues: non-pinned action Secure Hash Algorithms (SHAs), missing cache, inefficient matrix *(oss plugin required — skip if `$OSS_AVAILABLE` empty)*
- `oss:gh-scraper` → GitHub metadata extraction: pagination truncation, axis data completeness, rate-limit handling, scrape envelope correctness *(oss plugin required — skip if `$OSS_AVAILABLE` empty)*
- `oss:repo-warden` → vitality scoring issues: incorrect bus-factor approximation, bot-filtering inconsistency, axis scoring errors, PARTIAL_FILE overwrite conflicts *(oss plugin required — skip if `$OSS_AVAILABLE` empty)*
- `research:data-steward` → data issues: label leakage, split contamination, augmentation order bugs, API pagination truncation, dataset completeness, provenance gaps *(research plugin required — skip if `$RESEARCH_AVAILABLE` empty)*
- `research:scientist` → paper analysis: missed contributions, wrong method attribution *(research plugin required — skip if `$RESEARCH_AVAILABLE` empty)*
- `foundry:solution-architect` → design issues: leaky abstractions, circular dependencies, missing Architecture Decision Record (ADR), backward-compat violations without deprecation path
- `foundry:web-explorer` → content quality: broken or unverified Uniform Resource Locators (URLs), outdated docs, incomplete extraction from fetched pages
- `oss:shepherd` → Open Source Software (OSS) governance: incorrect Semantic Versioning (SemVer) decision, missing CHANGELOG entry, bad deprecation path, wrong release checklist item *(oss plugin required — skip if `$OSS_AVAILABLE` empty)*
- `foundry:challenger` → plan/architecture challenges: missed assumptions, missing edge cases, unjustified blocker classification, skipped refutation step
- `foundry:creator` → content quality: narrative arc gaps, audience-profile mismatches, voice inconsistency, missing story beats, out-of-scope format acceptance

All agents support `ceiling` difficulty tier. Ceiling patterns by domain: `foundry:sw-engineer` → adversarial (idiomatic-looking but subtly wrong), concurrency bugs; `foundry:qa-specialist` → incomplete detectability (coverage gaps visible only at runtime); `foundry:perf-optimizer` → deep cross-function control flow; `research:data-steward` → adversarial (split contamination disguised as correct preprocessing); `foundry:solution-architect` → deep dependency tracing. Agents where ceiling infeasible (e.g., `foundry:linting-expert` — violations always statically detectable): generators may substitute hard problem.

### Step 2: Spawn agent pipeline subagents

Mark "Calibrate agents" in_progress. **Availability check** (vars set in SKILL.md Step 2): skip `oss:*` agents if `$OSS_AVAILABLE` empty; skip `research:*` agents if `$RESEARCH_AVAILABLE` empty. Log: "<plugin> plugin not installed — skipping <agent> calibration" per excluded agent.

Per agent in domain table (after exclusions), spawn one `general-purpose` pipeline subagent. **Spawn in batches of `$PIPELINE_BATCH_SIZE` (5 when this category runs alone, 2 while two categories in flight — see constants)**: issue up to that many agent pipeline spawns per response, wait for all in batch to return compact JSON results, spawn next batch. Agents within a batch run concurrently; batches sequential. Do NOT spawn all agents in one response — 14+ agents spikes context and resource usage.

Resolve template dir first — no `~/.claude/skills/` copy exists (setup symlinks only `rules/*.md` and `TEAM_PROTOCOL.md`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
CALIB_TPL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" calibrate templates $([ "$LOCAL_MODE" = "true" ] && echo --local))  # timeout: 5000
```

Each subagent gets pipeline template from `$CALIB_TPL/pipeline-prompt.md`, substitutions:

- `<TARGET>` = agent name (e.g., `foundry:sw-engineer`)
- `<DOMAIN>` = domain string from table above
- `<N>` = 3 (fast) or 10 (full)
- `<TIMESTAMP>` = current run timestamp
- `<MODE>` = `fast` or `full`
- `<AB_MODE>` = `true` or `false` — whether to run A/B variant scoring against `general-purpose` baseline (see pipeline-prompt.md Phase 2b)
- `<LOCAL_MODE>` = `true` or `false` — from `--local` flag; when true pipeline resolves target file from source tree

Run dir per agent: `.reports/calibrate/<TIMESTAMP>/<TARGET>/`

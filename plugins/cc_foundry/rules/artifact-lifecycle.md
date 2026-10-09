---
description: Canonical artifact directory layout, run-dir naming convention, TTL policy, and plan/benchmark isolation from shipped content
paths:
  - '**'
---

## Artifact Layout (stub)

Runtime artifacts at **project root** dot-dirs — never inside `.claude/`:

- `.temp/<skill>/<ts>/` — intermediate/handover files · `.reports/<skill>/<ts>/` — final consolidated reports · `.plans/{blueprint,active,closed}` — specs/todos/results · `.notes/` — lessons, diary · `.cache/gh/` — GitHub API cache · `.experiments/`, `.developments/` — research/develop runs
- Run-dir timestamp: `$(date -u +%Y-%m-%dT%H-%M-%SZ)` (UTC, dashes, filesystem-safe); completed run contains `result.jsonl`
- TTL: dot-prefixed artifact dirs gitignored, auto-cleaned at 30 days; `.plans/active|closed` and `.notes/` manual — never auto-delete

## Evidence Isolation — Plans and Benchmarks Never Ship

- Plans, reports, scratch artifacts, private implementation notes, and benchmark task IDs/target repositories/prompt wording/expected answers/task-specific source or symbol examples are evidence, never production content: never copy them — or plan-only notation, section references, task IDs, private source or code examples, placeholder names, private shorthand — into shipped code, plugins, skills, templates, schemas, or user-facing docs; never make a shipped artifact depend on its originating `.plans/` or `.reports/` context
- Re-express every adopted requirement as a self-contained contract (descriptive names, neutral generic examples, all context to verify it without the plan); encode benchmark-derived behavior as a regression test, never as shipped examples

> Full rule (complete layout tree, per-dir TTL conditions, naming examples) in `_full/artifact-lifecycle.md`. **Read when defining new skill's output dirs or TTL behavior**:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/artifact-lifecycle.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/artifact-lifecycle.md"  # timeout: 5000
> ```

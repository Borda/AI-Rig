# Effort and timeout policy

> Caller input wins over classification. When effort is omitted from a skill invocation, calling skill selects `low` for a narrow mechanical change or focused factual question whose contract is already settled; `medium` for a bounded implementation, diagnosis, or review with a few interacting decisions; `high` for cross-file reasoning, adversarial review, ambiguous behavior, architecture, migration, or security judgment; and `xhigh` only for an unusually broad, consequential task that cannot be narrowed. `max` is explicit-caller-only. Direct CLI and MCP fallback is `medium` when no skill performed classification. An omitted model uses target host's configured default because bridge cannot infer locally available model identifiers.

## Effort validation

> Valid canonical levels are `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. Claude maps `minimal` to `low`; Codex accepts canonical spelling only when target model advertises it. A caller-supplied unknown level is rejected before spawning a child.

> When supplied `supported_efforts` excludes a valid requested level, choose nearest lower supported level once and return `effort_substituted` with requested, applied, and reason. If a stale capability report still yields a structured unsupported-effort failure, perform same one substitution retry for read-only verbs only; a failed write-capable child may already have landed edits, so implement reports fault instead of re-running. Never silently use a host default.

## Budgets and retry

> Only write-capable operation is `implement`, with a 600-second soft budget and no automatic retry because edits may have landed.

> Soft budgets are advise 120 seconds, review 300 seconds, and implement 600 seconds. Held process receives a hard cutoff at 1.2 times its soft budget. A timeout retries once only for advise or review, at the next lower supported effort tier, because retry keeps same budget and only a cheaper attempt can finish inside it; a request already at the lowest supported tier is reported without retry. Implement never retries automatically because edits may have landed.

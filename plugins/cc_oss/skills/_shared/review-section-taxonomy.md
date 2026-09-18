<!-- file: review-section-taxonomy.md — consumers: review/SKILL.md, resolve/SKILL.md -->

## Review Report Section Taxonomy

Canonical section headers, grep keys, agent ownership, resolve triage type. Both `review` (consolidator) and `resolve` (parser) load this file — edit here only.

| Section header | Grep key | Owner agent | resolve `type` (MEDIUM) | resolve `change` |
| -- | -- | -- | -- | -- |
| `### [blocking] Critical` | `Critical` / `[blocking]` | any | `[req]` | `code` (domain not distinguishable at this header — falls back to the general implementer) |
| `### Architecture & Quality` | `Architecture` | `foundry:sw-engineer` | `[req]` (code-related) | `code` |
| `### Test Coverage Gaps` | `Test Coverage` | `foundry:qa-specialist` | `[suggest]` | `test` |
| `### Performance Concerns` | `Performance` | `foundry:perf-optimizer` | `[req]` (code-related) | `perf` |
| `### Documentation Gaps` | `Documentation` | `foundry:doc-scribe` | `[suggest]` | `docs` |
| `### Static Analysis` | `Static Analysis` | `foundry:linting-expert` | `[suggest]` | `style` |
| `### Cosmetic / Style` | `Cosmetic` | `foundry:linting-expert` | `[suggest]` | `style` |
| `### API Design (if applicable)` | `API Design` | `foundry:solution-architect` | `[req]` (code-related) | `architecture` |
| `### Codex Co-Review` | `Codex Co-Review` | `codex` | `[suggest]` | `code` |
| `### OSS Checks` | (skip) | — | — | — |
| `### Issue Root Cause Alignment` | (skip) | — | — | — |
| `### Recommended Next Steps` | (skip) | — | — | — |
| `### Review Confidence` | (skip) | — | — | — |

`resolve change` feeds oss:resolve's Phase 2 specialist routing (`action-item-dispatch.md`) — keep in sync with that file's `change` → `IMPL_AGENT` table whenever either changes.

## Severity → Resolve Type

All severities:

| Severity | Section | resolve `type` |
| -- | -- | -- |
| CRITICAL or `[blocking]` | any | `[req]` |
| HIGH | any | `[req]` |
| MEDIUM | Architecture, Performance, API Design (code-related) | `[req]` |
| MEDIUM | Test Coverage, Documentation, Static Analysis, Codex Co-Review | `[suggest]` |
| LOW | any | `[suggest]` — group by topic, never drop (see below) |
| COSMETIC (`[cosmetic]` label) | any | `[suggest]` — severity 1; group per LOW Grouping Rule |

## LOW Grouping Rule

Never omit LOW items **present in the report** — this rule binds the resolve parser (extraction + AskUserQuestion clustering), not what the review consolidator chooses to write. Report-side pruning governed by review/checklist.md §Consolidation Rules. When total pending items > 12 (AskUserQuestion single-call ceiling), cluster LOW items into composite `[suggest]` rows by **topic or logical theme**. Cluster by semantic similarity, not by section or file. Each composite row:

- `summary`: cluster theme (≤55 chars)
- `change`: bullet list of every member finding with `file:line`
- `severity`: max member severity (1–2 for LOW)
- `full_comment_text`: concatenation of member bullets
- `file`/`line`: blank (multi-file)

Compress until total ≤ 12. Surface every LOW as own row when count permits, group only as needed.

## Grep Pattern (resolve parser)

```bash
grep -E '^### .*(Critical|\[blocking\]|Architecture|Test Coverage|Performance|Documentation|Static Analysis|Cosmetic|API Design|Codex Co-Review)'
```

Headers may carry `⚠ LOW CONFIDENCE — ` prefix — use contains-match, not exact-match.

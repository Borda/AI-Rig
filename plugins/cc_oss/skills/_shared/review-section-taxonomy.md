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
| `### Design Divergence` | (skip) | blind-solve (Agent 0) | — | — |
| `### OSS Checks` | (skip) | — | — | — |
| `### Issue Root Cause Alignment` | (skip) | — | — | — |
| `### Recommended Next Steps` | (skip) | — | — | — |
| `### Review Confidence` | (skip) | — | — | — |

`resolve change` feeds oss:resolve's Phase 2 specialist routing (`dispatch-prelude.md`) — keep in sync with that file's `change` → `IMPL_AGENT` table whenever either changes.

## Severity → Resolve Type

All severities:

| Severity | Section | resolve `type` |
| -- | -- | -- |
| any severity | `### [blocking] Critical` | `[req]` |
| CRITICAL | any | `[req]` |
| HIGH | any | `[req]` |
| MEDIUM | Architecture, Performance, API Design (code-related) | `[req]` |
| MEDIUM | Test Coverage, Documentation, Static Analysis, Codex Co-Review | `[suggest]` |
| LOW | all other sections | `[suggest]` — one item and one row per finding the report prints, never dropped (see below) |
| COSMETIC (`[cosmetic]` label) | all other sections | `[suggest]` — severity 1; same LOW Grouping Rule |

Root-cause-merged `merged_into` rows keep their own validated severity and section, but add no item: the survivor's item records `merged_finding_ids`, appends each merged row's text, takes the group's highest severity and becomes `[req]` if any member alone would be. A pending GitHub item at the survivor's or any merged row's location matches the survivor; its existing `type` stays unchanged. A `merged_into` naming no single new survivor keeps the row as its own item and emits a `⚠` stderr line the orchestrator relays to the user. Merge summary JSON reports `folded` (count) and `fold_orphans` (list of `{finding_id, merged_into}` records).

## LOW Grouping Rule

Never omit LOW items **present in the report** — this rule binds the resolve parser, not what the review consolidator chooses to write. Report-side pruning governed by review/checklist.md §Consolidation Rules.

- Every LOW finding the report prints stays its own item in `action-items.jsonl`, with its own stable `finding_id`, and its own row in resolve's selection table at every pending count; its type is `[req]` in `### [blocking] Critical`, otherwise `[suggest]`. A root-cause-merged row not printed as a separate finding folds into the survivor's item as described above; an orphan still keeps its own item and row.
- Never cluster LOW items into composite display rows. Resolve's selection gate (`hooks/enforce-resolve-table.js`) requires every pending and resolved/addressed id as its own table cell, so a composite row hides its member ids and the picker call is denied.
- Past 18 pending items (AskUserQuestion checkbox ceiling: 2 calls × 3 item questions × 3 items — the 4th slot of every call is the mandatory bulk page, never items), resolve drops per-item checkboxes for its compressed table, which still lists every item as its own row.

## Grep Pattern (resolve parser)

```bash
grep -E '^### .*(Critical|\[blocking\]|Architecture|Test Coverage|Performance|Documentation|Static Analysis|Cosmetic|API Design|Codex Co-Review)'
```

Headers may carry `⚠ LOW CONFIDENCE — ` prefix — use contains-match, not exact-match.

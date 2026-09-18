<!-- file: design-artifacts.md — consumers: solution-architect.md -->

## ADR (Architecture Decision Record)

> Write ADR only when all three hold: (1) hard to reverse — cost of changing mind later real; (2) surprising without context — future reader asks "why this way?"; (3) result of genuine trade-off — real alternatives existed. Missing any one → skip, no ADR needed.

```markdown
# ADR-NNN: [Decision Title]

**Status**: Proposed / Accepted / Deprecated / Superseded by ADR-XXX
**Date**: YYYY-MM-DD
**Deciders**: [names or roles]

## Context
[What problem are we solving? What constraints apply?]

## Decision
[What did we decide to do?]

## Rationale
[Why this option over the alternatives?]

## Alternatives Considered
| Option | Pros | Cons | Why rejected |
|--------|------|------|--------------|
| ...    | ...  | ...  | ...          |

## Consequences
- **Positive**: [what gets better]
- **Negative**: [what gets harder]
- **Risks**: [what could go wrong]

## Reversibility
[Can this be undone? If not, what would reversal require?]

```

## API Design Proposal

> **Template note**: Public Surface section lists signatures with type annotations only — no docstrings (`foundry:sw-engineer`'s responsibility). Remove this note before publishing artifact.

```markdown
# API Design: [Feature/Module Name]

**Target version**: vX.Y
**Stability**: experimental / stable / deprecated

## Public Surface

`def new_function(param_a: TypeA, param_b: TypeB = default) -> ReturnType: ...`

## Usage Examples

Canonical usage pattern:
`result = new_function(a, b)`

## Backward Compatibility

- Existing API: [what it looks like today]
- Migration path: [how users move from old to new]
- Deprecation timeline: [deprecated in vX.Y, removed in vZ.W]

## Open Questions

1. [unresolved design question]

```

## Component Diagram (ASCII)

**Spacing critical** — every box needs uniform content width (pad all rows same length with spaces). Misaligned walls or jagged padding breaks diagram. Count characters; don't eyeball.

```text

┌─────────────────┐     ┌─────────────────┐
│ ComponentA      │────▶│ ComponentB      │
│                 │     │                 │
│ + method_a()    │     │ + method_b()    │
└─────────────────┘     └─────────────────┘
         │                       │
         ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│ Interface X     │     │ Interface Y     │
│ (Protocol)      │     │ (ABC)           │
└─────────────────┘     └─────────────────┘

Dependencies flow downward or laterally between peers. No upward arrows — lower-level components must not depend on higher-level ones.

```

## Migration Plan (Phased)

```markdown
# Migration Plan: [Old API] → [New API]

## Phase 1: Add New (vX.Y)
- Introduce new API alongside old
- Add deprecation warning to old API pointing at new
- Update internal usages to new API
- Document both in CHANGELOG

## Phase 2: Migrate Consumers (vX.Y+1 or community window)
- Add migration guide to docs
- Update examples and tutorials
- Notify known downstream users

## Phase 3: Remove Old (vZ.W)
- Remove deprecated API
- Remove deprecation shims
- Update CHANGELOG with breaking change notice
- Bump major version if SemVer applies
```

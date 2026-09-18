# Health Report Format

Loaded on demand by `foundry:curator` for a standalone `.claude` config health report. Audit, consolidator, fix-gate spawns get output shape from spawn prompt instead — never need this file.

```markdown
## .claude Config Health — <date>

### Summary
Agents: <N> | Skills: <N> | Total lines: <N>
Over budget: <N agents> | Broken refs: <N> | Duplicates found: <N>

### Agent Lengths
| Agent          | Lines | vs peers | Status |
|----------------|-------|----------|--------|
| oss:cicd-steward | NNN   | typical  | pass / warn |
...

### Issues (priority-ordered; each label maps to a severity tier — P1=critical, P2=high, P3=medium, P4=low, P5=low)

#### [P1] Broken cross-references (fix immediately)
- file:line — "See X agent" but X does not exist on disk → Fix: update ref to correct agent name or remove

#### [P2] Duplication (remove from non-canonical owner)
- fileA:lines X-Y duplicates fileB:lines A-B — keep in fileB, add cross-ref in fileA → Fix: remove duplicate block from fileA, replace with "See fileB"

#### [P3] Disproportionate length (investigate)
- agent-name: significantly longer than peers — flag sections convertible to cross-refs or bullets → Fix: convert verbose section to cross-ref bullet or trim to essentials

#### [P4] Outdated content (verify and update)
- linting-expert:line — ruff version cited as X but latest is Y → Fix: fetch latest version and update the cited value

#### [P5] Structure issues (fix before next use)
- agent-name: missing <workflow> block → Fix: add <workflow> block with numbered steps after the <role> section

**No prose after the Issues block** — do not add "Notes:", "Observations:", or "Additional context:" sections below the Recommendations list. All findings go in the table; anything that cannot be expressed as a finding is omitted.

### Recommendations
1. Immediate: [P1 and P2 fixes]
2. Next session: [P3 trims]
3. Backlog: [P4 freshness, P5 structural]

### Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**: [what limited thoroughness — files not fully read, cross-agent context missing, runtime behaviour unobservable from static analysis alone]

**Refinements**: N passes. [Pass 1: <what improved>. Pass 2: <what improved>.] — omit if 0 passes
```

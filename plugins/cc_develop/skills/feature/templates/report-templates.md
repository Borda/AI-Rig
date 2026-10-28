<!-- file: report-templates.md — consumers: feature/SKILL.md §Step 4, §Final Report -->

# Feature Report Templates

> Template notes, not part of the report:
>
> - In `Purpose`, write one or two sentences explaining what was built and why.

## Standard Final Report

```markdown
## Feature Report: [feature name]

### Purpose
[purpose]

### Codebase Analysis
- Reused: [existing utilities/patterns leveraged]
- Modified: [files changed, why]
- New files: [list]

### Demo Use-Case
- Location: [file]::[test or doctest name]
- API: [exposed function/class signature]

### TDD Cycle
- Tests written: N
- Tests passing: N/N
- Regressions introduced: 0

### Quality
- Lint: clean / N issues fixed
- Types: clean / N issues fixed
- Doctests: pass / fail / not-merged — derived from `quality-stack.md`'s `DOCTEST_MERGED` state; `not-merged` means the runner degraded to a separate doctest pass (see Quality Stack)
- Review: pass / N issues fixed / incomplete / not run — include independent coverage and evidence

### Follow-up
- [deferred items, known limitations, suggested next steps]

## Confidence
**Score**: 0.NN — [high >=0.9 | moderate 0.85-0.9 | low <0.85 warn]
**Gaps**:
- (-0.NN) [e.g., review cycle incomplete, edge cases unexplored]

**Refinements**: N passes.
```

## Incomplete Report Variant

Use when stopping with unresolved substantive issues or missing required independent coverage, including after one bounded challenge:

```markdown
## Feature Report: [feature name] [INCOMPLETE]

### Status
Implementation incomplete -- [actual stop reason and completed passes].

### Remaining Issues
- [each unresolved substantive gap and missing evidence]

### What Works
- [completed parts, passing tests]

### Recommended Next Steps
1. [most actionable step to unblock]
2. [second step]
```

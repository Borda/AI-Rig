<!-- file: report-templates.md — consumers: feature/SKILL.md §Step 4, §Final Report -->

# Feature Report Templates

## Standard Final Report

```markdown
## Feature Report: <feature name>

### Purpose
[1-2 sentences: what built, why]

### Codebase Analysis
- Reused: [existing utilities/patterns leveraged]
- Modified: [files changed, why]
- New files: [list]

### Demo Use-Case
- Location: <file>::<test or doctest>
- API: [exposed function/class signature]

### TDD Cycle
- Tests written: N
- Tests passing: N/N
- Regressions introduced: 0

### Quality
- Lint: clean / N issues fixed
- Types: clean / N issues fixed
- Doctests: passing
- Review: pass / N issues fixed (N cycles)

### Follow-up
- [deferred items, known limitations, suggested next steps]

## Confidence
**Score**: 0.N — [high >=0.9 | moderate 0.85-0.9 | low <0.85 warn]
**Gaps**:
- [e.g., review cycle incomplete, edge cases unexplored]

**Refinements**: N passes.
```

## Incomplete Report Variant

Use when stopping after 3 review cycles with unresolved substantive issues:

```markdown
## Feature Report: <feature name> [INCOMPLETE]

### Status
Implementation incomplete -- stopped after 3 review cycles.

### Remaining Issues
- [each unresolved substantive gap]

### What Works
- [completed parts, passing tests]

### Recommended Next Steps
1. [most actionable step to unblock]
2. [second step]
```

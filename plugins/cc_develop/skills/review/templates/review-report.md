---
develop-review:  [target — file / dir / working-tree diff]
Date:         [YYYY-MM-DD]
Change Type:  [fix | feat | refactor | perf | docs | ci | chore | test | mixed — from change intent, not file count or commit message]
Scope:        [key changed files, comma-separated]
Focus:        [SCOPE-LABEL — one-line description of what the change does]
Agents:       [comma-separated agent names that ran]
CI:           N/A (develop:review is read-only — runs no tests)
Outcome:      [APPROVE | NEEDS_WORK | REQUEST_CHANGES]
Summary:      [1–2 sentence overview of key findings]
Confidence:   [aggregate score] — [key gaps]
Next steps:   [comma-separated actionable items — blockers first]
Path:         → .reports/review/<YYYY-MM-DDTHH-MM-SSZ>/review-report.md
---

## Code Review: [target]

### [blocking] Critical (must fix before merge)

- [bugs, security issues, data corruption risks]
- Every finding carries explicit severity: `[cosmetic]` `[low]` `[medium]` `[high]` `[critical]`

### Architecture & Quality

- [sw-engineer findings]
- [blocking], [nit] marked explicit

### Test Coverage Gaps

- [qa-specialist findings — top 5 missing tests]
- ML code: non-determinism, missing seed issues

### Performance Concerns

- [perf-optimizer findings — ranked by impact]
- Include: current behavior vs expected improvement

### Documentation Gaps

- [doc-scribe findings]
- Public API without docstrings, listed explicit

### Static Analysis

- [linting-expert findings — ruff violations, mypy errors, annotation gaps]

### Cosmetic / Style

(omit if none)

- [cosmetic findings — pure style/whitespace/formatting, no behaviour change]

### API Design (if applicable)

- [solution-architect findings — coupling, API surface, backward compat]
- Public API changes: [intentional / accidental leak]
- Deprecation path: [provided / missing]

### Codex Co-Review

(omit if Codex unavailable or no unique findings)

- [unique findings from codex.md, not already in agent sections]
- Duplicates (same location as agent finding): omitted — see agent section

### Recommended Next Steps

1. [most important action]
2. [second most important]
3. [third]

### Review Confidence

| Agent | Score | Label | Gaps |
| -- | -- | -- | -- |

**Aggregate**: min 0.N / median 0.N

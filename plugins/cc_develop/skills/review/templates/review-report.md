---
develop-review:  [review target]
Title:        develop-review — [review target]
Date:         [YYYY-MM-DD]
Change Type:  [fix | feat | refactor | perf | docs | ci | chore | test | mixed]
Scope:        [key changed files]
Focus:        [scope label — change summary]
Agents:       [agents that ran]
Reviewers:    [Role (rating), Role (rating)]
CI:           N/A (develop:review is read-only — runs no tests)
Outcome:      [APPROVE | NEEDS_WORK | REQUEST_CHANGES]
Summary:      [review summary]
Confidence:   [aggregate score] — [key gaps]
Next steps:   [actions, blockers first]
Path:         → .reports/review/[YYYY-MM-DDTHH-MM-SSZ]/review-report.md
---

Legend: 1 = Approve · 2 = Minor changes · 3 = Changes required · 4 = Insufficient evidence · 5 = Block / Reject.

## Code Review: [review target]

[Preserve the aggregate review summary here as prose, including overall verdict and material limits.]

### Findings overview

| ID | Author | Finding | Resolution proposal | Status |
| -- | -- | -- | -- | -- |
| [stable finding ID] | [all contributing reviewer roles] | [short problem] | [concrete proposal] | [required / minor / verify] |

> Keep existing sections below. Reference the same finding IDs; this overview adds attribution without removing detail.

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

> Template notes, not part of the report:
>
> - Use a file, directory, or working-tree diff as the review target.
> - Classify `Change Type` by intent using `fix`, `feat`, `refactor`, `perf`, `docs`, `ci`, `chore`, `test`, or `mixed`; never classify by file count or commit message.
> - List key changed files in `Scope`, comma-separated, and keep `Focus` to one line.
> - List agent names that ran, comma-separated; list actual reviewers as readable `Role (rating)` entries.
> - Use `APPROVE`, `NEEDS_WORK`, or `REQUEST_CHANGES` for `Outcome`; summarize key findings in 1–2 sentences.
> - Put blockers first in `Next steps` and list no more than five, as specified in `consolidator-prompt.md`.

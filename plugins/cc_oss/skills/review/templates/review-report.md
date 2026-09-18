---
Title:       oss-review — [PR #N title]
PR:          #[N]
Date:        [YYYY-MM-DD]
PR Type:     [fix | feat | refactor | perf | docs | ci | chore | test | mixed — from change intent, not file count or PR title]
Scope:       [key changed files, comma-separated]
Focus:       [SCOPE-LABEL — one-line description of what the change does]
Agents:      [comma-separated agent names that ran]
CI:          [passing (N/N) / failing — check-name, check-name / pending]
Gate:        [PASS | BLOCK | REJECT_<GROUND> @<sha> — GROUND one of GOAL/CONDUCT/SCOPE/LICENSE/DUPLICATE/REVERTED/SPAM/PHILOSOPHY, see review SKILL.md Stage 1; PASS/BLOCK reach full review, REJECT_* carries reviewed commit SHA so /oss:resolve can detect whether PR has since changed]
Outcome:     [APPROVE | NEEDS_WORK | REQUEST_CHANGES | N/A — rejected at gate]
Summary:     [1–2 sentence overview of key findings]
Confidence:  [aggregate score] — [key gaps]
Next steps:  [comma-separated actionable items — blockers first]
Path:        → .reports/review/pr-<N>/run-<NNN>/review-report.md
---

## Code Review: [target]

### [blocking] Critical (must fix before merge)

- [bugs, security issues, data corruption risks]
- Every finding carries explicit severity: `[cosmetic]` `[low]` `[medium]` `[high]` `[critical]`

### Issue Root Cause Alignment

(omit if no linked issues)

- Issue #N: [title] — [root cause hypothesis from analysis]
- Root cause addressed: [yes / partially / no — explanation]
- PR/issue scope alignment: [aligned / diverged — what differs]
- Reproduction tested: [yes / no — what's missing]

### Architecture & Quality

- [sw-engineer findings]
- [blocking] issues marked explicitly
- [nit] suggestions marked explicitly

### Test Coverage Gaps

- [qa-specialist findings — top 5 missing tests]
- ML code: non-determinism or missing seed issues

### Performance Concerns

- [perf-optimizer findings — ranked by impact]
- Include: current behavior vs expected improvement

### Documentation Gaps

- [doc-scribe findings]
- Public API without docstrings listed explicitly

### Static Analysis

- [linting-expert findings — ruff violations, mypy errors, annotation gaps]

### Cosmetic / Style

(omit if none)

- [cosmetic findings — pure style/whitespace/formatting, no behaviour change]

### API Design (if applicable)

- [solution-architect findings — coupling, API surface, backward compat]
- Public API changes: [intentional / accidental leak]
- Deprecation path: [provided / missing]

### OSS Checks

- New deps: [list, license status]
- API stability: [public API removed without deprecation?]
- CHANGELOG: [updated / not updated]
- Secrets scan: [clean / found: file:line]

### Codex Co-Review

(omit if Codex unavailable or no unique findings)

- [unique findings from codex.md not in agent sections above]
- Duplicate findings (same location as agent finding): omitted — see agent section

### Recommended Next Steps

1. [most important action]
2. [second most important]
3. [third]

### Review Confidence

| Agent | Score | Label | Gaps |
| -- | -- | -- | -- |

**Aggregate**: min 0.65 / median 0.N [⚠ LOW CONFIDENCE: qa-specialist could not verify test execution — treat coverage findings as indicative, not conclusive]

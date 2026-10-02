---
Title:       oss-review — [PR #N title]
PR:          #[N]
Date:        [YYYY-MM-DD]
PR Type:     [type]
Scope:       [key changed files, comma-separated]
Focus:       [SCOPE-LABEL]
Impact:      [FULL|LIGHT] · [reason]
Agents:      [comma-separated agent names that ran]
Reviewers:   [Role (rating), Role (rating)]
CI:          [CI status]
Gate:        [gate result]
Outcome:     [review outcome]
Summary:     [summary]
Confidence:  [aggregate score] — [key gaps]
Next steps:  [next steps]
Path:        → .reports/review/pr-<N>/run-<NNN>/review-report.md
---

Legend: 1 = Approve · 2 = Minor changes · 3 = Changes required · 4 = Insufficient evidence · 5 = Block / Reject.

> - PR Type: `fix`, `feat`, `refactor`, `perf`, `docs`, `ci`, `chore`, `test`, or `mixed`, chosen by change intent rather than file count or title.
> - Focus: one-line description of the change, labeled with its scope.
> - Reviewers: readable role names with scoped integer ratings, such as `Software engineer (3), QA specialist (2)` or `sw-engineer: 3, qa-specialist: 2`; never a bare `sw-engineer 3`.
> - CI: `passing (N/N)`, `failing — check-name, check-name`, or `pending`.
> - Gate: `PASS`, `BLOCK`, or `REJECT_<GROUND> @<sha>`, where GROUND is `GOAL`, `CONDUCT`, `SCOPE`, `LICENSE`, `DUPLICATE`, `REVERTED`, `SPAM`, or `PHILOSOPHY` (review SKILL.md Stage 1). PASS/BLOCK continue to full review. The `@<sha>` suffix on a `REJECT_<GROUND>` gate carries the reviewed commit SHA so `/oss:resolve` can detect whether the PR changed.
> - Outcome: `APPROVE`, `NEEDS_WORK`, `REQUEST_CHANGES`, or `N/A` when rejected at the gate. Write the verdict token alone, optionally after its `✓`/`⚠`/`✗` symbol; severity counts and other detail belong in Summary.
> - Summary: 1–2 sentences describing key findings. Next steps are comma-separated actionable items, blockers first.

## Code Review: [target]

[aggregate summary]

> Keep the aggregate summary as prose, including the overall verdict and material limits.

### Findings overview

| ID | Author | Finding | Resolution proposal | Status |
| -- | -- | -- | -- | -- |
| [finding ID] | [reviewer roles] | [finding] | [proposal] | [status] |

> Use the stable finding IDs minted into `findings.jsonl` (section slug plus a hash of file and title) across this overview and the detailed sections. Author lists all contributing reviewer roles; Resolution proposal is concrete. Status is `required`, `minor`, or `verify`.

### [blocking] Critical (must fix before merge)

- [critical findings]
- Every finding carries one severity label: `[cosmetic]`, `[low]`, `[medium]`, `[high]`, or `[critical]`.

> Include bugs, security issues, and data corruption risks.

### Issue Root Cause Alignment

(omit if no linked issues)

- Issue #N: [issue title] — [root cause]
- Root cause addressed: [yes / partially / no]
- PR/issue scope alignment: [aligned / diverged]
- Reproduction tested: [yes / no]

> State the root-cause hypothesis from analysis. Explain what differs when scope is diverged; explain partial/no answers and missing reproduction evidence.

### Architecture & Quality

- [sw-engineer findings]
- [blocking] issues marked explicitly
- [nit] suggestions marked explicitly

> Cover architecture and quality findings.

### Test Coverage Gaps

- [QA findings]
- ML code: non-determinism or missing seed issues

> List the top 5 missing tests.

### Performance Concerns

- [performance findings]
- Include: current behavior vs expected improvement

> Rank performance concerns by impact.

### Documentation Gaps

- [doc-scribe findings]
- Public API without docstrings listed explicitly

> List public APIs lacking docstrings explicitly.

### Static Analysis

- [static analysis findings]

> Include Ruff violations, mypy errors, and annotation gaps.

### Cosmetic / Style

(omit if none)

- [cosmetic findings]

> Include only pure style, whitespace, or formatting changes with no behavior change.

### API Design (if applicable)

- [API design findings]
- Public API changes: [intentional / accidental leak]
- Deprecation path: [provided / missing]

> Cover coupling, API surface, and backward compatibility.

### OSS Checks

- New deps: [list, license status]
- API stability: [public API removed without deprecation?]
- CHANGELOG: [updated / not updated]
- Secrets scan: [clean / found: file:line]

### Codex Co-Review

(omit if Codex unavailable or no unique findings)

- [unique Codex findings]
- Duplicate findings (same location as agent finding): omitted — see agent section

> Include only unique findings from `codex.md` that are absent from the agent sections.

### Recommended Next Steps

1. [highest-priority action]
2. [next action]
3. [next action]

### Review Confidence

| Agent | Score | Label | Gaps |
| -- | -- | -- | -- |

**Aggregate**: min 0.65 / median 0.N [⚠ LOW CONFIDENCE: qa-specialist could not verify test execution — treat coverage findings as indicative, not conclusive]

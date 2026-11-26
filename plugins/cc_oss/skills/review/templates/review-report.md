---
Title:       oss-review — [PR #N title]
PR:          #[N]
Link:        [full PR URL]
Date:        [YYYY-MM-DD]
Commit SHA:  [full frozen reviewed PR head, or explicit unavailable reason]
PR Type:     [type]
Scope:       [key changed files, comma-separated]
Focus:       [SCOPE-LABEL]
Impact:      [FULL|LIGHT] · [reason]
Agents:      [comma-separated agent names that ran]
Review contract: 1
Rating scale: source-5-v2
Readiness:   [complete | blocked | incomplete]
Reviewers:   Role [rating], Role [rating]
CI:          [CI status]
Gate:        [gate result]
Outcome:     [review outcome]
Summary:     [summary]
Confidence:  [aggregate score] — [key gaps]
Next steps:  [next steps]
Path:        → .reports/review/pr-<N>/run-<NNN>/review-report.md
---

Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.

Axes: Gate = CI merge gate at the reviewed head, or a premise rejection · Outcome = source judgment · Readiness = verification state; each axis is read on its own.

> - PR Type: `fix`, `feat`, `refactor`, `perf`, `docs`, `ci`, `chore`, `test`, or `mixed`, chosen by change intent rather than file count or title.
> - Focus: one-line description of the change, labeled with its scope.
> - Reviewers: readable role names only with square-bracketed scores, such as `Software engineer [3], QA specialist [2]`; omit execution-origin labels.
> - CI: Final same-head observation, including pending, failed, cancelled or skipped checks, no reported checks, or the reason current status is unavailable.
> - Gate: the CI merge gate, independent of Outcome and Readiness (`modes/verdict-states.md`): `REVIEW_V1_PASS`, `REVIEW_V1_BLOCK`, or `REVIEW_V1_REJECT_<GROUND> @<sha>`, where GROUND is `GOAL`, `CONDUCT`, `SCOPE`, `LICENSE`, `DUPLICATE`, `REVERTED`, `SPAM`, or `PHILOSOPHY` (review SKILL.md Stage 1). PASS/BLOCK continue to full review. The `@<sha>` suffix on a `REJECT_<GROUND>` gate carries the reviewed commit SHA so `/oss:resolve` can detect whether the PR changed.
> - Outcome: `APPROVE`, `MINOR_EDITS`, `MAJOR_REVISION`, `INSUFFICIENT_BUT_PROMISING`, `REJECT`, or literal `null`. Write the verdict token alone, optionally after its `✓`/`⚠`/`✗` symbol; severity counts and other detail belong in Summary.
> - Summary: opens `Intended behavior: <yes/partly/no/undetermined>`, then 1–2 sentences describing key findings. Next steps are comma-separated actionable items, blockers first.

## Code Review: [target]

[aggregate summary]

> Keep the aggregate summary as prose, including the overall verdict and material limits. Open it with `Intended behavior: <yes/partly/no/undetermined> — <basis>`.

### Findings overview

| ID | Author | Finding | Resolution proposal | Status |
| -- | -- | -- | -- | -- |
| [finding ID] | [reviewer roles] | [finding] | [proposal] | [status] |

> Use the stable finding IDs minted into `findings.jsonl` (section slug plus a hash of file and title) across this overview and the detailed sections. Author lists all contributing reviewer roles; Resolution proposal is concrete. Status is `required` or `minor`; unverified concerns appear separately.

### [blocking] Critical (must fix before merge)

- Only `[critical]`, `[high]` per checklist.md, or any `[blocking]`-tagged finding per SKILL.md §Block-tier catalogue
- Every finding carries one severity label: `[cosmetic]`, `[low]`, `[medium]`, `[high]`, or `[critical]`.

> This section sets placement only; `checklist.md` and the parent's validation set severity. Every finding below `[high]` without a `[blocking]` tag belongs in its dimension section, not here; a `[blocking]`-tagged finding lands here at its own severity.

### Issue Root Cause Alignment

(omit if no linked issues)

- Issue #N: [issue title] — [root cause]
- Root cause addressed: [yes / partially / no]
- PR/issue scope alignment: [aligned / diverged]
- Reproduction tested: [yes / no]

> State the root-cause hypothesis from analysis. Explain what differs when scope is diverged; explain partial/no answers and missing reproduction evidence.

### Architecture & Quality

- [sw-engineer findings]
- `[blocking]`-tagged findings go to `### [blocking] Critical`, not here
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

### Open Concerns

[One row per open_concerns entry in review-validation.json: candidate ID, uncertainty, missing prerequisite or feasible undone check, owner, next check, observable closure. These are not confirmed defects or automatic fix requests.]

### Recommended Next Steps

1. [highest-priority action]
2. [next action]
3. [next action]

### Review Confidence

| Agent | Score | Label | Gaps |
| -- | -- | -- | -- |
| Codex co-reviewer | — | [confidence_null_reason; the reply's numeric confidence replaces the dash only when status is complete] | [confidence_gaps lines, or status and confidence_null_reason when not complete] |

> Insert the parent's generated `RERUN_HINTS` lines verbatim. Each starts with `Unreviewed dimension:` and names the exact unit from `lineup.md`. An unslotted promoted unit has a missing assessment for every role listed on its lineup row. Omit the lines when the generated output is empty.

**Environment**: `<status> · python <env_python> · installed N · gated N · failed N · host <os> <native_arch> <cpu_model>, <cpu_count> cores, <N> GiB, accelerators <list or none>` — one line from `review-env.json`; the host is a fact of this run, never a gap. Only a class a claim or code path requires and this host lacks is written, as `not reproducible on this host: requires <CLASS>; <probe evidence>`.

**Author claim**: `<claim_text> — <verdict> · Δ <relative_delta> · spread <spread> · <comparable_hardware.decision> · <rounds_completed>/<method.repeats> rounds, <method.execution> · <reason>` — one line per claim from `claims-result.json`; `not-run` is an execution gap with its reason; omit when the PR states no claim.

**Ephemeral dependency evidence**: `<pkg>==<ver> · wheel <filename> · spec base <file:line> head <file:line> '<spec>' · fetch <full command> · run <full command> · result <N passed, M failed> · env unchanged · routing default index (<sources checked>)` — one line per overlay run, or the failed step + concrete reason; omit when no claim needed a missing package.

**Aggregate**: min 0.65 / median 0.N / ceiling 0.N (<binding coverage gap>) → 0.N [⚠ LOW CONFIDENCE: qa-specialist could not verify test execution — treat coverage findings as indicative, not conclusive]

> Numeric scores only: a Codex lane without an envelope confidence, or a null, failed or blocked reply, never enters min or median. The final value is the median capped by the lowest applicable ceiling in `modes/verdict-states.md` §Confidence ceilings and equals the header `Confidence:` score.

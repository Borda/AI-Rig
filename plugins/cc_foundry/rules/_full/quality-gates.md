---
description: Output quality standards — Confidence block, link verification, output routing
paths:
  - '**'
---

## Proportional Execution

Small implementation: concise plan → brief parent self-challenge (not independent) → accept challenged plan within existing authority → assign parent execution for indivisible work or useful bounded disjoint owners → implement core acceptance → consolidate actual source and handoffs, one bounded independent result challenge, verification and outcome. Medium/large: one scoped plan → independent plan challenge → bounded higher-capability advice only for material unresolved uncertainty → approve challenged plan within existing authority → delegate useful bounded disjoint work or retain indivisible parent execution → implement → consolidate integrated result, independent implementation challenge and verification against every criterion. Approval never creates a routine user prompt: ask only for actual missing authority, protected decisions or an explicit user approval gate. Reuse still-current parent plans and challenges for covered children; do not repeat formal planning ceremonies for already accepted bounded leaves; decompose outcomes that are not yet bounded. Name core acceptance and deferred scope; expand only for demonstrated current need, reuse valid evidence, and stop or re-scope waste. No standalone plan artifact, specialist bundle or automatic loop solely for size. Preserve actual independence, permissions, safety, read-only boundaries, explicit user waivers, single-pass remediation with pre-edit selection, and workflow gates. Report post-challenge fixes, their verification and missing independent coverage honestly.

As the operational default for the plan → challenge → approve → delegate → consolidate lifecycle above, first validate that the task is a coherent bounded outcome: clear inputs, owner, scope, and acceptance, with no unresolved design or dependency requiring further decomposition. Turn unresolved causes, dependencies, or material design choices into bounded investigation or decision prerequisites before implementation; decomposition does not itself resolve them. If the task is not yet bounded, split it into smaller outcomes and validate each piece; a fix plus its regression stays one leaf, without per-file or per-line fragmentation. Keep a compact task map in the current plan or notes and detail only the active piece, its dependencies, and interfaces. Work depth-first: complete the active piece's applicable implementation, required independent result challenge, and acceptance verification before opening the next dependent piece. Delegate useful dependency-ready siblings only with agreed stable interfaces and disjoint ownership; serialize coupled work, parent integration, and shared acceptance. If the active piece is blocked, record its exact owner and next action and continue only independent ready work. For each open criterion, record the smallest missing outcome and next edit, check, evidence reconciliation, or user decision; near-closure selection is a recovery heuristic, and optional discoveries stay separate. Reuse evidence only while its source, scope, inputs, dependencies, and required independence remain valid; run only missing or invalidated checks. After each bounded handoff, the parent reconciles actual source, handover, and evidence, records verified closure or the exact remaining owner and action, then selects work under the dependency and closure rules above and checks the consolidated parent outcome. A bookkeeping or artifact pass closes only its proven obligation; required source fixes, independent coverage, source-bound or live checks, and final project gates remain mandatory.

## Evidence Grounding

**Scope** — any of: technical constraints ("X cannot do Y", "not supported by", "requires workaround"), feasibility assumptions ("this approach will work because", "X is fast/reliable enough"), recalled facts from memory or training ("I know that X does Y", "typically Z", "this library usually"), behavioral assumptions ("this function returns", "this API expects", "this version changed").

**Evidence authority — not all sources are equal**

Tier 1 — Authoritative (sufficient alone): official documentation (versioned), source code read from disk, official release notes / changelogs, specification or RFC from governing body, test suite output from this session.

Tier 2 — Weak (requires ≥3 independent sources OR experimental validation): blog posts, tutorials, Stack Overflow, forum posts, social media, third-party summaries, training knowledge. Only Tier 2 available: find ≥3 genuinely independent corroborating sources, OR run a minimal experiment that empirically confirms or refutes the premise. Document which path was taken.

**Independence requirement**: sources are independent only when derived from different authors and different primary research — not when they cite each other or all trace back to a common origin. N posts all referencing the same blog post = 1 source, not N. Count unique origin nodes, not surface-level citations.

**Citation tracing — mandatory before counting sources**:

1. For each Tier 2 source: follow its citations and references one level deep
2. Map each source to its origin: `source → cites → origin`
3. Singleton detection: ≥2 sources share the same origin → merge into one; count distinct origins only
4. Tier upgrade: tracing reveals a Tier 1 source (official doc, spec, changelog) a Tier 2 source cited but wasn't found directly → read that Tier 1 source; confirms the claim → premise now Tier 1 verified (sufficient alone)
5. After tracing, distinct-origin count < 3 and no Tier 1 found → require experimental validation

## Python Code Complexity

Full per-limit rationale (stub keeps the numbers + violation consequence):

- **Cyclomatic complexity ≤12** — >12 independent paths → extract sub-functions or introduce guard clauses
- **Required arguments (no default) ≤7** — primary rule, enforced in review; >7 required params = introduce a config dataclass; kwargs with defaults may exceed 7 freely; ruff `PLR0913` set to ≤12 as a blunt total-args backstop
- **Branches ≤12** — >12 `if`/`elif`/`match` arms → dispatch table or strategy pattern
- **Statements ≤50** — >50 logical lines in one function → split responsibility
- **Return points ≤6** — >6 `return` statements → consolidate early-return paths

Applies to all Python written or reviewed by any agent.

## Output Routing

**Branch-slug expression** for the `.temp/output-*.md` filename: `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`.

**Follow-up gate options** — skill-defined; minimum: (a) primary action · (b) skip. Canonical examples by skill:

- `foundry:audit` → (a) `/foundry:setup` (sync clean config) · (b) fix all findings · (c) skip
- `foundry:distill` → (a) `/foundry:manage create` (scaffold suggestion) · (b) edit existing · (c) skip

## Report File Format

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md (stub, canonical rule text + marker), plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates.md — this section is worked-example detail only; the restated policy statement lives in the stub. -->

**Required minimum fields** (all reports):

```yaml
---
Title:      [Skill] — [subject]
Date:       [YYYY-MM-DD]
Scope:      [what was analyzed — file paths, topic, PR#, run-id, etc.]
Focus:      [aspect examined — "quality audit" / "SOTA research" / "code review" / etc.]
Agents:     [agent names that contributed — comma-separated]
Outcome:    [verdict — ✓ APPROVED | ✓ READY | ⚠ NEEDS_ATTENTION | ✗ BLOCKED | etc.]
Confidence: [score] — [key gaps]
Next steps: [recommended follow-up skill invocation]
Path:       → .reports/<skill>/<timestamp>/<name>.md
---
```

After required fields, add skill-specific fields relevant to the report type (e.g. Verdict, CI, Risk, Blockers for `develop:review`; Best method, Papers for `research:topic`; Methodology, Findings for `research:judge`). `develop:review`'s report template is the canonical reference. Skills with dedicated output routing (audit, review, resolve, analyse, release) must include an equivalent `---` block atop their report files.

**Terminal render of the block above** (one row per key, in file order, values verbatim — no re-wrapping, no truncation):

```markdown
| Field | Value |
| --- | --- |
| Title | [Skill] — [subject] |
| Date | [YYYY-MM-DD] |
| Scope | [what was analyzed] |
| Focus | [aspect examined] |
| Agents | [agent names] |
| Outcome | [✓/⚠/✗ verdict] |
| Confidence | [score] — [key gaps] |
| Next steps | [recommended follow-up] |
| Path | → .reports/<skill>/<timestamp>/<name>.md |
```

## Adversarial Convergence Loop

The mandatory procedure is `_full/adversarial-loop.md`. This example demonstrates continued strictly decreasing scores beyond three reviews.

### Worked example

A refactor stays within unchanged scope and reviewed coverage. Each later review verifies the prior signatures fixed and discovers distinct additional signatures; none reaches a third open occurrence:

| Iteration | Findings | `W_n` | `r_n` | Reading |
| -- | -- | -- | -- | -- |
| 0 | 1 critical, 2 high, 3 medium, 4 low | 10 + 12 + 12 + 8 = 42 | — | first review |
| 1 | 1 high, 2 medium, 3 low | 6 + 8 + 6 = 20 | 0.48 | converging — continue |
| 2 | 2 low, 1 nit | 4 + 1 = 5 | 0.25 | converging — continue |
| 3 | 1 low | 2 | 0.40 | converging — continue |
| 4 | none | 0 | 0 | independently clean — complete remaining caller gates |

The third review has open findings but still decreases the total integer score, so it does not trigger a generic round cap. Continue only with valid authority, independence, current snapshots and caller budgets; plateau, increase or a third occurrence of the same invariant (same signature or same invariant evidence) still stops the affected route. The final zero needs current independent coverage and the owning workflow's post-clean gates. Preserve every completed row and any earlier stopped history.

### Challenger Rounds

A challenger round that returns no deliverable or a `blocked` envelope is an agent failure, never a silent `not-run`: record its cause from the transcript (safety-classifier stop, refused report write, background stall, malformed envelope, reply truncated before its required closing block) and re-dispatch once with a brief that removes it. For a security-filter review, leave Codex enabled (no `--no-codex`): the challenger runs the open-ended bypass search through its Codex cross-check, statically classifies Codex's candidates and any probe generator the caller supplies, counts the reproduced ones in its own six-tier row, and never authors bypass strings itself, so the round needs no separate Codex row.

## Pre-Handover Check

Trigger is a **specific unproven claim**, not a score crossing a line: a premise no source was read for, a conclusion resting on one ambiguous signal, an alternative never examined. First close it with available evidence within the authorized scope. A low score whose gaps are all already documented needs no dispatch — state the factual limits and hand over. If resolving the claim requires a review outside that scope, ask once before dispatch with the exact gap, expected evidence/output, and stop condition; dispatch only if the user explicitly chooses it. A declined or dismissed optional review is skipped without another ask or a request in findings/action items; retain only the factual limitation needed to describe what remains unverified. One accepted pass does not authorize a follow-up review or challenge loop. When authorized, render and call `Skill(skill="bridge:review", args="Read-only adversarial review of <exact area and target paths>. Uncertain claims: <complete claim list>. Current evidence: <source paths or observations>. Challenge each claim, identify missing evidence and alternatives, and return actionable findings with locations; do not apply fixes.")`; never pass placeholders or a workflow step label. Incorporate returned evidence before handover. Bridge absent or disabled → state the specific gap so the user can decide whether to authorize another supported route.

## Write-Delegation Checklist (`bridge:implement`)

Before calling `bridge:implement`, construct a complete brief containing the exact finding, target paths, current evidence, permitted edits, required result, stop condition, and verification command. Clean the git tree first (`git status -sb` — a dirty tree blocks; it makes the diff impossible to isolate). After it returns, read the **full diff** yourself, run the actual proof command. Repeated fix rounds on the same issue (2+) → stop delegating, finish by hand. Commit only after your own diff read plus proof run; the bridge never commits.

## Reporting Findings — Block Merge Integrity

After merging two blocks (combining e.g. `<antipatterns>` + `<quality-checks>` into one), diff the combined output against both originals; every named rule (`##` heading or bold title) must survive; zero silent drops.

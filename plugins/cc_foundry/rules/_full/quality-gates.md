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

**Long output** (multi-item analysis, 5+ findings — including lists of 5+ items: module names, issues, files —, or prose >~10 lines) → two mandatory steps, in order:

1. **Write tool call** — create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` (new file — never overwrite; append a counter suffix if the slug exists, e.g. `-2.md`); full evidence coverage, ultra-caveman compressed (see §Prose Compression — "full" means no dropped findings, not verbose prose). **Execute the Write tool call; don't narrate intent and proceed without calling it** — never skipped; pipeline/background mode returns missing decisions to its owner (step 2d), but never exempts this Write. Distinct from any other file write the task also does.
2. Print to terminal, in order:
   - a. **YAML header table** — render the `---` metadata block as a two-column Markdown table (`Field | Value`, one row per key, each value on a single physical line ≤100 chars — a wrapped value loses its leading `|` and breaks GFM table parsing from that row down); never print raw YAML (see §Report File Format). No YAML block → fall back to a plain ASCII verdict line, `·` separator: `verdict: ⚠ NEEDS_WORK · findings: 8 · ...` (verdict word prefixed with its symbol — see §Reporting Findings).
   - b. **Report path** — `→ <filepath>`.
   - c. **Executive summary** — 2–3 sentence overview + every critical/high finding listed individually; omit medium/low detail unless ≤2 total findings.
   - d. **Conditional follow-up gate** — invoke `AskUserQuestion` only when a required decision or authorization is missing; output length never triggers a question. Continue an already-authorized next action in the same turn. Background/pipeline work returns any missing decision to its owner.

- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; do **not** create a file
- **Copy-intent override**: output destined for an external artifact (PR body, release notes, report to share) → write to file regardless of length; output read in-context and acted on immediately (audit findings, calibration result, code review) → terminal only even if long
- **Follow-up precedence**: this conditional rule overrides generic skill completion prompts such as `NEVER SKIP` or `Always fires` when no required decision or authorization is missing. Preserve concrete gates for a new fix scope, paid execution, sensitive or destructive actions, remote changes, and other ungranted authority; existing authorization applies only within its granted scope. Do not invent a decision merely to fill a follow-up menu.
- **Follow-up gate follow-through**: selected option triggers a skill → call `Skill(skill=..., args=...)` in the same turn; never narrate intent as prose ("Invoke that next.") and stop without acting
- **Don't ask what you can't honor**: selected option can't trigger automatic action (`disable-model-invocation: true`, or output is intermediate with a downstream AskUserQuestion coming anyway) → print the suggestion as plain text instead of asking a hollow question

### Additional worked detail

**Branch-slug expression** for the `.temp/output-*.md` filename: `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`.

**Follow-up gate options** — skill-defined; minimum: (a) primary action · (b) skip. Canonical examples by skill:

- `foundry:audit` → (a) `/foundry:setup` (sync clean config) · (b) fix all findings · (c) skip
- `foundry:distill` → (a) `/foundry:manage create` (scaffold suggestion) · (b) edit existing · (c) skip

## Report File Format

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates.md, plugins/cc_foundry/rules/_full/quality-gates.md -->

**Universal terminal-print rule**: when a skill or agent writes a report file whose first non-whitespace line is `---` (YAML metadata block), that block MUST be rendered in the terminal as a **simple two-column Markdown table** (`Field | Value`, one row per YAML key, in file order) — never dumped as raw YAML — as the **first content of the reply**, before the report path, before the executive summary, before anything else. Applies to ALL skills and agents producing such reports, no per-skill restatement needed. The table IS the reply header; omit the `╔═╗` Re:Anchor box when leading with it (see `communication.md` exemption).

Every report file created via output routing begins with this YAML `---`-delimited block; it stays raw YAML on disk (machine-parseable by downstream skills) and is converted to the table only for the terminal print.

**Value length cap — single physical line only**: every field value ≤100 chars, one line, no embedded wrap (a soft-wrapped value breaks GFM table parsing from that row down). Long detail (`Focus`, `Summary`) belongs in the prose executive summary below the table, not the cell.

**Required minimum fields** (all reports): `Title`, `Date`, `Scope`, `Focus`, `Agents`, `Outcome`, `Confidence`, `Next steps`, `Path`. Add skill-specific fields after (e.g. Verdict/CI/Risk/Blockers for `develop:review`). Skills with dedicated output routing (audit, review, resolve, analyse, release) must include an equivalent `---` block at the top of their report files.

_Outcome legend_: `✓` = approved/ready/clean · `⚠` = needs-attention/needs-work · `✗` = blocked/rejected. Distinct from `!`, reserved for standalone alert blocks (`! BREAKING`, `**! BLOCKED**`) — see §Reporting Findings.

### Additional worked detail

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

A request to repeat review and fix until a reviewer reports no findings or no security findings, or to resolve all findings; a second independent review round about to be dispatched for the same change; or a skill workflow repeating its review then fix cycle counts as an explicitly invoked loop from that moment: before the next reviewer dispatch, start ledger; follow `_full/adversarial-loop.md` (helper output, stop/plateau rules, user's round budget).

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

Before calling `bridge:implement`, construct a complete brief containing the exact finding, target paths, current evidence, permitted edits, required result, stop condition, and verification command. Clean the git tree first (`git status -sb` — a dirty tree blocks; it makes the diff impossible to isolate) unless the invoking skill explicitly documents a combined-change exception with bounded owned paths, a recorded starting snapshot, combined-diff review and proof commands; only that invocation may use the exception. After it returns, read the **full diff** yourself, run the actual proof command. Repeated fix rounds on the same issue (2+) → stop delegating, finish by hand. A review-and-fix loop counts every round that fixes the same component as one issue, even when each round's findings differ; continuing past the second delegated round on one component needs one recorded user decision naming the component, never an assumed carry-over of the first delegation approval. Commit only after your own diff read plus proof run; the bridge never commits.

## Reporting Findings — Block Merge Integrity

After merging two blocks (combining e.g. `<antipatterns>` + `<quality-checks>` into one), diff the combined output against both originals; every named rule (`##` heading or bold title) must survive; zero silent drops.

## Confidence Block (required on all analysis tasks)

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md -->

Name the process or claim being assessed. Assess the correctness of the delivered result and how it was produced and checked within its stated scope; do not substitute likelihood of later task completion. When process confidence is requested, score the process rather than certainty about status. Report completion status separately: unfinished work alone does not determine confidence, and a high score never marks incomplete work complete. Scores for acceptance or handover gates must assess the relevant process or substantive judgment; status certainty alone cannot satisfy them. Deduct only for evidence gaps that materially limit the assessed claim; retain other relevant limitations with `(-0.00)`.

Every analysis agent **must** end with:

```markdown
## Confidence
**Score**: 0.NN — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- (-0.NN) [specific limitation]
- (-0.NN) residual — [uncertainty no named gap explains]   ← only when needed
                          ← blank line required; Refinements is a peer field, not a sub-bullet
**Refinements**: N passes.
- Pass 1: [what gap was addressed — must name the gap, not just say "re-checked"]
```

> **Never skip** — missing Confidence block = rule violation.

- Omit **Refinements** if 0 passes (don't write "0 passes") — omit individual **Gaps** bullets only at Score 1.0, but keep the **Gaps** header
- **Score**, **Gaps**, **Refinements** = peer top-level fields — never nest Refinements under Gaps; blank line before **Refinements** required
- Score < 0.85 → ⚠ on the score line AND on the line immediately after (standalone, not a Gaps bullet): "review limitations are explicit; no extra review is authorized by this score alone"
- Gaps = factual review limits. They do not become source findings, remediation tasks, or authorization for another review. Any review beyond the invoked workflow's authorized scope needs one explicit user decision naming the exact gap, expected evidence/output, and stop condition. If declined or dismissed, skip it without asking again or carrying the request into findings/action items; retain only the factual limitation needed to describe what was not verified.
- **Each Gap opens with its deduction** `- (-0.NN) <gap>`: its share of the `1.0 - Score` shortfall — an attribution, approximate when gaps overlap. Deductions sum to exactly `1.0 - Score`, so a reader can drop or re-weigh a gap and re-assess the score. A cap, floor, or band that set the score is itself a gap: name it, carry its delta. A limitation that did not lower the score stays as `(-0.00)`. Uncertainty no named gap explains goes in one `residual` bullet — never spread silently across gaps. ASCII `-`, two decimals. Score below 1.0 with no Gaps bullets = rule violation

## Evidence and real-case acceptance

- Every factual, causal, and completion claim must attach its Evidence Grounding source or recorded proof at the claim. Keep conclusions within that evidence: source text proves only what it states; passing unit tests prove only their exercised contract, not an untested workflow or host. Missing proof stays blocked or unverified.
- For costly, nondeterministic, or external workflow validation, first run realistic controlled cases with independently specified expected outcomes, relevant failure and recovery cases, and negative controls. Only after all applicable controlled cases pass, run bounded representative real or live validation. Controlled cases and test doubles do not prove live or independent behavior. Examine an actual incident as needed for diagnosis.
- Keep a representative real case alongside TDD: retain available pre-fix incident evidence and, after controlled cases pass, verify the intended result with real inputs, components and affected environment; retain commands, environment/source identity, outputs and before/after evidence, and recheck before completion. An unavailable real environment remains an unmet acceptance check. Never weaken assertions, remove failing coverage, or change expected outputs or user-authorized behavior merely to turn tests green. Correct a mistaken test only from independent specification or real-case evidence, record why, and preserve the intended contract; a changed requirement needs the user's decision.

## Prose Compression — Output Files

Applies to all agents; compression tier by destination. Cap is a **soft compression target, not a truncation trigger** — never drop evidence, findings, or CRITICAL/HIGH content to force a file under cap. Compress prose (articles, filler, hedging, verbose framing) first; still over cap after full compression → let it run over rather than lose substance. Structurally large artifacts (multi-agent aggregates, batch reports) legitimately exceed the target — a signal the content warrants its size. Only LOW/Nitpick-severity items are droppable for space; CRITICAL and HIGH always survive intact.

The soft-cap escape isn't licence to run long. Written deliverables skew longer on current models than the caps assume, so match document length to what the task needs: cover the substance, add no filler sections, redundant summaries, restated findings, or boilerplate. Length earned by evidence is fine; length earned by padding is not.

Size estimate: `$(( $(wc -c < file) / 3 ))` tokens — `/4` predates the current tokenizer and under-reports by roughly 30%.

- `.reports/` (human review) — **normal caveman**, ~10K tokens (~500 lines) target: drop articles/filler/hedging; full sentences where clarity demands; fragments OK for terse findings
- `.temp/` (consolidator handover) — **ultra caveman**, ~10K tokens (~500 lines) target: fragments only, zero filler, shortest synonyms, ~30–40% tighter than normal caveman; full evidence coverage still required — compress prose, not substance

## Internal Quality Loop (analysis tasks only)

**No routine re-read pass.** Current models mostly catch their own errors without a mandated re-score cycle; forcing one anyway compounds token cost without reliably improving results. Write once, full care.

A second pass fires only on a **named, actionable gap** — a source not read, a claim not grounded, a section the ask required and the draft lacks. Address that gap, record it under **Refinements**. Generic phrases ("re-checked, looks fine", "reviewed for completeness") aren't gaps, never justify a pass or a score rise. Cap 2. Report the real score — never inflate; `foundry:calibrate` catches bias.

Gaps that can't be closed (info-access limits, tooling absent) are **documented in the Confidence block, not chased** — caveat and move on.

## Reporting Findings

- **Coverage at the finding stage, filtering downstream**: report every issue found, including low-confidence and low-severity ones; attach confidence and severity so a later stage can rank. Severity words in output-routing rules ("omit medium/low detail") govern the **printed summary**, never what gets investigated or recorded — a finding dropped at discovery can't be recovered by a filter. Never instruct an agent to "only report high-severity issues" or "be conservative": current models follow that literally, investigating just as deeply and reporting less
- **Report before fixing**: state every finding before any fix — never silently mutate
- **Per-fix narration**: before each file edit or tool call, state what changes and why
- **! BREAKING format**: breaking findings = standalone block — never inline or buried in a table row:

```text
! BREAKING — <one-line impact: what breaks and who is affected>
Fix: <concrete action to resolve>
```

- Severity markers: `!` = critical (standalone alert-block prefix only, e.g. `! BREAKING`) · `⚠` = warnings · `✓` = pass · hint = fix hint. Outcome/verdict tables use `✗` for blocked/rejected instead (§Report File Format) — `!` never appears as a table-cell symbol, only as the alert-block prefix
- **Block merge integrity** (trigger: merging two instruction blocks): diff combined output against both originals — every named rule survives, zero silent drops; worked detail in `_full/quality-gates.md` §Reporting Findings — Block Merge Integrity
- **Deferred work must appear in the delivered artifact**: if any analysis, rubric definition, or implementation is deferred, approximated, or left incomplete, document it explicitly in the output file ("Phase 2 / requires X / not yet implemented") — not only in conversation

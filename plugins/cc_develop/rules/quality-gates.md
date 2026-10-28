---
description: Output quality standards — Confidence block, link verification, output routing
paths:
  - '**'
---

## Evidence and real-case acceptance

- Every factual, causal, and completion claim must cite a source actually inspected, a recorded experiment, or other concrete proof at the claim. Label inference and hypothesis explicitly; absent proof means blocked or unverified, never success. Source text proves only what it states; passing unit tests prove only their exercised contract, not an untested workflow or host.
- For costly, nondeterministic, or external workflow validation, first run realistic controlled cases with independently specified expected outcomes, relevant failure and recovery cases, and negative controls. Only after all applicable controlled cases pass, run bounded representative real or live validation. Controlled cases and test doubles do not prove live or independent behavior. Examine an actual incident as needed for diagnosis.
- Keep a representative real case alongside TDD: retain available pre-fix incident evidence and, after controlled cases pass, verify the intended result with real inputs, components and affected environment; retain commands, environment/source identity, outputs and before/after evidence, and recheck before completion. An unavailable real environment remains an unmet acceptance check. Never weaken assertions, remove failing coverage, or change expected outputs or user-authorized behavior merely to turn tests green. Correct a mistaken test only from independent specification or real-case evidence, record why, and preserve the intended contract; a changed requirement needs the user's decision.

## Proportional Execution

Small implementation: concise plan → brief parent self-challenge (not independent) → accept challenged plan within existing authority → assign parent execution for indivisible work or useful bounded disjoint owners → implement core acceptance → consolidate actual source and handoffs, one bounded independent result challenge, verification and outcome. Medium/large: one scoped plan → independent plan challenge → bounded higher-capability advice only for material unresolved uncertainty → approve challenged plan within existing authority → delegate useful bounded disjoint work or retain indivisible parent execution → implement → consolidate integrated result, independent implementation challenge and verification against every criterion. Approval never creates a routine user prompt: ask only for actual missing authority, protected decisions or an explicit user approval gate. Reuse still-current parent plans and challenges for covered children; do not repeat formal planning ceremonies for already accepted bounded leaves; decompose outcomes that are not yet bounded. Name core acceptance and deferred scope; expand only for demonstrated current need, reuse valid evidence, and stop or re-scope waste. No standalone plan artifact, specialist bundle or automatic loop solely for size. Preserve actual independence, permissions, safety, read-only boundaries, explicit user waivers, single-pass remediation with pre-edit selection, and workflow gates. Report post-challenge fixes, their verification and missing independent coverage honestly.

As the operational default for the plan → challenge → approve → delegate → consolidate lifecycle above, first validate that the task is a coherent bounded outcome: clear inputs, owner, scope, and acceptance, with no unresolved design or dependency requiring further decomposition. Turn unresolved causes, dependencies, or material design choices into bounded investigation or decision prerequisites before implementation; decomposition does not itself resolve them. If the task is not yet bounded, split it into smaller outcomes and validate each piece; a fix plus its regression stays one leaf, without per-file or per-line fragmentation. Keep a compact task map in the current plan or notes and detail only the active piece, its dependencies, and interfaces. Work depth-first: complete the active piece's applicable implementation, required independent result challenge, and acceptance verification before opening the next dependent piece. Delegate useful dependency-ready siblings only with agreed stable interfaces and disjoint ownership; serialize coupled work, parent integration, and shared acceptance. If the active piece is blocked, record its exact owner and next action and continue only independent ready work. For each open criterion, record the smallest missing outcome and next edit, check, evidence reconciliation, or user decision; near-closure selection is a recovery heuristic, and optional discoveries stay separate. Reuse evidence only while its source, scope, inputs, dependencies, and required independence remain valid; run only missing or invalidated checks. After each bounded handoff, the parent reconciles actual source, handover, and evidence, records verified closure or the exact remaining owner and action, then selects work under the dependency and closure rules above and checks the consolidated parent outcome. A bookkeeping or artifact pass closes only its proven obligation; required source fixes, independent coverage, source-bound or live checks, and final project gates remain mandatory.

For permitted independent review in `/develop:review` → `/develop:fix`, `/develop:debug` root-cause loops, pre-commit review. Permitted independent pass: dispatch `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` if available; never `subagent_type: "fork"`. Give reviewer diff, spec, symptom — never implementation narrative. Challenger round with no deliverable or a `blocked` envelope = agent failure, never silent `not-run`: record cause from transcript (safety-classifier stop, refused report write, background stall, malformed envelope, reply truncated before its required closing block), re-dispatch once with brief removing it. Security-filter review: leave Codex on (no `--no-codex`); challenger runs open-ended bypass search via its Codex cross-check, statically classifies Codex candidates + any caller probe generator, counts reproduced ones in its own six-tier row, never authors bypass strings; no separate Codex row.

## Adversarial Convergence Loop (explicitly invoked)

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md (canonical), plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, AGENTS.md -->

An explicitly invoked iterative convergence workflow retains all its existing rules. Read `_full/adversarial-loop.md` before that loop: scope, evidence ledger, strictly decreasing nonnegative integer score after baseline `W_0`, independent final snapshot, score weights (`20/10/6/4/2/1`), trend, remediation rules — all mandatory.

For the explicitly invoked loop, never fork the implementing conversation for review or close a local fix before later independent verification. Resolve every feasible authorized finding; a structural flag alone is not a stop, and a repeated invariant (same signature or same invariant evidence) requires shared-root-cause investigation before another fix. Any score decrease converges (`0 < r_n < 1`); equal scores plateau and increases do not converge. Unavailable independent coverage, stale final evidence, an unmet approval boundary, plateau/non-convergence with open findings, or exhaustion of a stricter caller budget stops the dependent route. Open `security` or `critical` findings forbid completion and commit; unresolved `high` findings require escalation rather than deferral. At a stop, report only completed-round scores (for example `W_0 → W_1 → W_2`), or `not-run` when no review completed, plus per-tier residue, evidence, owner and next action; invoke `AskUserQuestion` only for the concrete missing decision or authorization. A clean loop still requires the owning workflow's remaining gates.

After each validated round, print the progress table exactly: `| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |`, all rows kept, each cell `old + new`; never reshape or reduce it.

## Confidence Block (required on all analysis tasks)

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_foundry/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md -->

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

## Internal Quality Loop (analysis tasks only)

**No routine re-read pass.** Current models mostly catch own errors without mandated re-score cycle; forcing one anyway compounds tokens, doesn't reliably improve. Write once, full care.

1. Second pass fires only on **named, actionable gap** — source not read, claim not grounded, section the ask required and draft lacks
2. Address that gap, record under **Refinements**; generic phrases ("re-checked, looks fine", "reviewed for completeness") not gaps — never justify pass or score rise
3. Gaps unclosable (info-access limits, tooling absent) → document in Confidence block, not chased
4. Cap 2 passes. Report real score — never inflate

## Pre-Handover Check

Trigger is a **specific unproven claim**, not a score crossing a line: premise no source read for, conclusion resting on one ambiguous signal, alternative never examined. Low score with every gap already documented → no dispatch; say so and hand over.

First close the claim with available evidence within the authorized scope. If resolving it requires a review beyond that scope, ask once before dispatch with the exact gap, expected evidence/output, and stop condition; dispatch only if the user explicitly chooses it. A declined or dismissed optional review is skipped without another ask or a request in findings/action items; retain only the factual limitation needed to describe what remains unverified. One accepted pass does not authorize a follow-up review or challenge loop. When authorized and `bridge@borda-ai-rig` is available, render and call `Skill(skill="bridge:review", args="Read-only adversarial review of <exact area and target paths>. Uncertain claims: <complete claim list>. Current evidence: <source paths or observations>. Challenge each claim, identify missing evidence and alternatives, and return actionable findings with locations; do not apply fixes.")`; never pass placeholders or a workflow step label. Incorporate returned evidence before handover. If the bridge is absent or disabled, state the gap and score; let the user decide whether to authorize another supported route.

## Link Verification

**Never add a URL without all four steps, every time — no exemption for domain/protocol/path similarity to an already-verified URL:**

1. **Fetch** — call WebFetch (or equivalent); URL must return non-error (not 4xx/5xx). HTTP 200 is necessary but not sufficient — steps 2 and 3 still mandatory
2. **Read** — read the actual page content; don't rely on URL structure or HTTP status alone
3. **Match** — confirm content matches the intended description; no match = don't add the link
4. **Independent** — every URL needs its own Fetch+Read+Match pass; a verified URL on the same domain doesn't exempt others; skipping any step — including inferring validity from URL structure or HTTP status alone — is a violation

Applies to: agent files, skill files, CLAUDE.md, any markdown.

## Output Routing

- **Long output** (multi-item analysis, 5+ findings — including lists of 5+ items: module names, issues, files —, or prose >~10 lines) → two mandatory steps in order:

1. Call **Write tool** to create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` where `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` (new file — never overwrite; append counter suffix if slug exists, e.g. `-2.md`); file gets **full content**
2. Print to terminal in this order:
   - a. **YAML header table** — render `---` metadata block as two-column Markdown table (`Field | Value`, one row per key, each value single physical line ≤100 chars — never wrap a value inside a cell: wrapped continuation line loses leading `|`, breaks GFM table parsing from that row down) — never print raw YAML verbatim (see **Report File Format** below); no YAML block → fall back to plain ASCII verdict line with `·` separator: `verdict: ⚠ NEEDS_WORK · findings: 8 · ...` (verdict word prefixed with its symbol — see §Reporting Findings)
   - b. **Report path** — `→ <filepath>`
   - c. **Executive summary** — prose: 2–3 sentence overview + each critical/high finding listed individual; omit medium/low detail unless ≤2 total findings
   - d. **Conditional follow-up gate** — invoke `AskUserQuestion` only when a required decision or authorization is missing; output length never triggers a question. Continue an already-authorized next action in the same turn. Background/pipeline work returns any missing decision to its owner.

- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; **no** file
- **Copy-intent override**: output destined for an external artifact (PR body, release notes, report to share) → write to file regardless of length; output read in-context and acted on immediately (audit findings, calibration result, code review) → terminal only even if long
- Prose paragraphs: no hard line breaks at column width
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. Canonical examples by skill:
  - `develop:review` → (a) walk through findings · (b) skip; `/develop:fix` and `/develop:refactor` printed as plain-text next steps
  - `develop:debug` → no gate; `/develop:fix --diagnosis <file>` printed as a plain-text next step
  - `develop:plan` → (a) Proceed — print the `/develop:feature --plan <file>` or `/develop:fix --plan <file>` handoff line · (b) Revise plan
- **Follow-up precedence**: this conditional rule overrides generic skill completion prompts such as `NEVER SKIP` or `Always fires` when no required decision or authorization is missing. Preserve concrete gates for a new fix scope, paid execution, sensitive or destructive actions, remote changes, and other ungranted authority; existing authorization applies only within its granted scope. Do not invent a decision merely to fill a follow-up menu.
- **Follow-up gate follow-through**: `AskUserQuestion` return with skill-invocation option selected → call `Skill(skill=..., args=...)` same response turn; never narrate intent as prose and stop without act
- **Don't ask what you can't honor**: selected option can't trigger automatic action (`disable-model-invocation: true`, or output is intermediate with a downstream AskUserQuestion coming anyway) → print the suggestion as plain text instead of asking a hollow question

## Report File Format

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates.md, plugins/cc_foundry/rules/_full/quality-gates.md -->

Every report file from output routing must begin with YAML metadata block between `---` delimiter lines. Block = canonical meta summary — file keeps raw YAML (machine-parseable by downstream skills); printed to terminal, convert to two-column table (`Field | Value`, one row per key) before executive summary — never raw YAML in terminal.

**Value cap — single line only**: each value ≤100 chars, one physical line, no wrap. Wrapped cell loses leading `|` on continuation line → parser drops table from that row down. Long detail (Focus, Summary) → short label in cell, full text in prose exec summary below.

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

_Outcome legend: `✓` = approved/ready/clean · `⚠` = needs-attention/needs-work · `✗` = blocked/rejected. Distinct from `!`, which is reserved for standalone alert blocks (`! BREAKING`) — see §Reporting Findings._

After required fields, add **skill-specific fields** for report type (e.g. Verdict, CI, Risk, Blockers for `develop:review`; Best method, Papers for `research:topic`; Methodology, Findings for `research:judge`). `develop:review` report template = canonical reference. Skills with dedicated output routing (audit, review, resolve, analyse, release) must include equivalent `---` block at top of report files.

## Reporting Findings

- **Coverage at the finding stage, filtering downstream**: report every issue found, including low-confidence and low-severity; attach confidence + severity so later stage ranks. Severity words in output-routing ("omit medium/low detail") govern **printed summary** only — never what gets investigated or recorded; finding dropped at discovery not recoverable by filter. Never instruct agent to "only report high-severity issues" or "be conservative" — current models follow literally, investigate just as deep, report less
- **Report before fixing**: state every finding before any fix — never silent mutate
- **Per-fix narration**: before each file edit or tool call, state what change and why
- **! BREAKING format**: breaking finding = standalone block — never inline or buried in table row:

```text
! BREAKING — <one-line impact: what breaks and who is affected>
Fix: <concrete action to resolve>
```

- Severity markers: `!` = critical (standalone alert-block prefix only, e.g. `! BREAKING`) · `⚠` = warnings · `✓` = pass · hint = fix hint. Outcome/verdict tables use `✗` for blocked/rejected instead (§Report File Format) — `!` never appears as a table-cell symbol, only as the alert-block prefix
- **Block merge integrity** (trigger: merging two instruction blocks): diff combined output against both originals — every named rule survives, zero silent drops
- **Deferred work must appear in the delivered artifact**: if any analysis, rubric definition, or implementation is deferred, approximated, or left incomplete, document it explicitly in the output file ("Phase 2 / requires X / not yet implemented") — not only in conversation
- Terminal colors: RED = critical · YELLOW = warnings · GREEN = pass · CYAN = fix hint

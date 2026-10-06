---
description: Output quality standards — Confidence block, link verification, output routing
paths:
  - '**'
---

## Evidence and real-case acceptance

- Every factual, causal, and completion claim must cite a source actually inspected, a recorded experiment, or other concrete proof at the claim. Label inference and hypothesis explicitly; absent proof means blocked or unverified, never success. Source text proves only what it states; passing unit tests prove only their exercised contract, not an untested workflow or host.
- Keep a representative real case alongside the TDD loop. Establish the failure and intended result with real inputs, real components, and the actual affected environment before relying on a test double; retain commands, environment/source identity, outputs and before/after evidence. Recheck that real case after the fix and before completion. An unavailable real environment remains an explicit unmet acceptance check; mocks cannot replace it or justify a completion claim.
- Mocks belong only to subsequent polishing after the real behavior is understood and validated. Use fast regression tests to shorten iteration while the real case remains the acceptance oracle. Never weaken assertions, remove failing coverage, or change expected outputs or user-authorized behavior merely to turn tests green. Correct a mistaken test only from independent specification or real-case evidence, record why, and preserve the intended contract; a changed requirement needs the user's decision.

## Adversarial Convergence Loop (any review → fix cycle)

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md (canonical), plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, AGENTS.md -->

Governs `/develop:review` → `/develop:fix`, `/develop:debug` root-cause loops, pre-commit review. Permitted independent pass: dispatch `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` if available; never `subagent_type: "fork"`. Give reviewer diff, spec, symptom — never implementation narrative.

Read `_full/adversarial-loop.md` before every independent review → authorized-fix cycle. Scope, evidence ledger, strictly decreasing nonnegative integer score after baseline `W_0`, independent final snapshot, score weights (`20/10/6/4/2/1`), trend, remediation rules — all mandatory.

Never fork the implementing conversation for review or close a local fix before later independent verification. Resolve every feasible authorized finding; a structural flag alone is not a stop, and a repeated signature requires shared-root-cause investigation before another fix. Any score decrease converges (`0 < r_n < 1`); equal scores plateau and increases do not converge. Unavailable independent coverage, stale final evidence, an unmet approval boundary, plateau/non-convergence with open findings, or exhaustion of a stricter caller budget stops the dependent route. Open `security` or `critical` findings forbid completion and commit; unresolved `high` findings require escalation rather than deferral. At a stop, report only completed-round scores (for example `W_0 → W_1 → W_2`), or `not-run` when no review completed, plus per-tier residue, evidence, owner and next action; invoke `AskUserQuestion` only for the concrete missing decision or authorization. A clean loop still requires the owning workflow's remaining gates.

## Confidence Block (required on all analysis tasks)

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_foundry/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md -->

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
- Score < 0.85 → ⚠ on the score line AND on the line immediately after (standalone, not a Gaps bullet): "orchestrator may re-run with the specific gap addressed"
- Gaps = primary signal — surfaces implicit limitations for re-run decisions
- **Each Gap opens with its deduction** `- (-0.NN) <gap>`: its share of the `1.0 - Score` shortfall — an attribution, approximate when gaps overlap. Deductions sum to exactly `1.0 - Score`, so a reader can drop or re-weigh a gap and re-assess the score. A cap, floor, or band that set the score is itself a gap: name it, carry its delta. A limitation that did not lower the score stays as `(-0.00)`. Uncertainty no named gap explains goes in one `residual` bullet — never spread silently across gaps. ASCII `-`, two decimals. Score below 1.0 with no Gaps bullets = rule violation

## Internal Quality Loop (analysis tasks only)

**No routine re-read pass.** Current models mostly catch own errors without mandated re-score cycle; forcing one anyway compounds tokens, doesn't reliably improve. Write once, full care.

1. Second pass fires only on **named, actionable gap** — source not read, claim not grounded, section the ask required and draft lacks
2. Address that gap, record under **Refinements**; generic phrases ("re-checked, looks fine", "reviewed for completeness") not gaps — never justify pass or score rise
3. Gaps unclosable (info-access limits, tooling absent) → document in Confidence block, not chased
4. Cap 2 passes. Report real score — never inflate

## Pre-Handover Check

Trigger is a **specific unproven claim**, not a score crossing a line: premise no source read for, conclusion resting on one ambiguous signal, alternative never examined. Low score with every gap already documented → no dispatch; say so and hand over.

When triggered and `bridge@borda-ai-rig` available → render and call `Skill(skill="bridge:review", args="Read-only adversarial review of <exact area and target paths>. Uncertain claims: <complete claim list>. Current evidence: <source paths or observations>. Challenge each claim, identify missing evidence and alternatives, and return actionable findings with locations; do not apply fixes.")`; never pass the placeholders or a workflow step label. Incorporate findings before handover. If the bridge is absent or disabled → state the gap and score explicitly so the user can re-run.

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
  - `develop:review` → (a) `/develop:fix` · (b) `/develop:refactor` · (c) walk through findings · (d) skip
  - `develop:debug` → (a) `/develop:fix --diagnosis <file>` · (b) skip
  - `develop:plan` → (a) `/develop:feature --plan <file>` · (b) `/develop:fix --plan <file>` · (c) skip
- **Follow-up precedence**: this conditional rule overrides generic skill completion prompts such as `NEVER SKIP` or `Always fires` when no required decision or authorization is missing. Preserve concrete gates for a new fix scope, paid execution, sensitive or destructive actions, remote changes, and other ungranted authority; existing authorization applies only within its granted scope. Do not invent a decision merely to fill a follow-up menu.
- **Follow-up gate follow-through**: `AskUserQuestion` return with skill-invocation option selected → call `Skill(skill=..., args=...)` same response turn; never narrate intent as prose and stop without act
- **Don't ask what you can't honor**: selected option can't trigger automatic action (`disable-model-invocation: true`, or output is intermediate with a downstream AskUserQuestion coming anyway) → print the suggestion as plain text instead of asking a hollow question

## Report File Format

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates.md -->

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

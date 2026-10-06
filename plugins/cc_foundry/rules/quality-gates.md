---
description: Output quality standards — Confidence block, link verification, output routing
paths:
  - '**'
---

> §Evidence Grounding, §Python Code Complexity, §Output Routing, §Report File Format have worked detail (tier tables, citation tracing, per-limit rationale, exact bash/example snippets) in `_full/quality-gates.md`; §Pre-Handover Check and §Write-Delegation Checklist live there in full (trigger-scoped — read at their trigger, summaries below). Resolve + Read when that section's own trigger applies — not needed for routine work:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/quality-gates.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/quality-gates.md"  # timeout: 5000
> ```

## Evidence Grounding (universal)

**Never generate without grounding in evidence** — every claim, finding, URL, or fact: read source, run command, check file first. No hypothesis as fact, no URL unverified, no finding unread. "Obvious"/"well-known"/session recall/training knowledge are **never** evidence — current disk state beats all of them. Evidence inaccessible → state `unable to verify: [reason]` explicitly, never substitute recall or inference.

**Design premises gate at entry, not delivery** — any assumption, constraint claim, or recalled fact used as a pillar of a design/implementation decision must be grounded in evidence read now, when it first enters the design. "Where is this documented?" first — no answer = unverified = no design built on it.

**Evidence tiers**: Tier 1 (official docs, source code read from disk, release notes, spec/RFC, this-session test output) — sufficient alone. Tier 2 (blog posts, tutorials, forums, training knowledge) — needs ≥3 genuinely independent sources OR a confirming experiment; independence + citation-tracing rules in `_full/quality-gates.md` §Evidence Grounding (read before counting Tier 2 sources).

## Adversarial Pass (all generation)

While producing output — not only after — ask "what would make this wrong?". Code: trace the failure path, not just the happy path (off-by-one, stale API, the edge case real data actually hits). Claims: separate "verified" from "pattern-matched" — verify or hedge the latter explicitly.

## Evidence and real-case acceptance

- Every factual, causal, and completion claim must attach its Evidence Grounding source or recorded proof at the claim. Keep conclusions within that evidence: source text proves only what it states; passing unit tests prove only their exercised contract, not an untested workflow or host. Missing proof stays blocked or unverified.
- Keep a representative real case alongside the TDD loop. Establish the failure and intended result with real inputs, real components, and the actual affected environment before relying on a test double; retain commands, environment/source identity, outputs and before/after evidence. Recheck that real case after the fix and before completion. An unavailable real environment remains an explicit unmet acceptance check; mocks cannot replace it or justify a completion claim.
- Mocks belong only to subsequent polishing after the real behavior is understood and validated. Use fast regression tests to shorten iteration while the real case remains the acceptance oracle. Never weaken assertions, remove failing coverage, or change expected outputs or user-authorized behavior merely to turn tests green. Correct a mistaken test only from independent specification or real-case evidence, record why, and preserve the intended contract; a changed requirement needs the user's decision.

## Adversarial Convergence Loop (any review → fix cycle)

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, AGENTS.md -->

Read `_full/adversarial-loop.md` before every independent review → authorized-fix cycle. Its scope, evidence ledger, strictly decreasing nonnegative integer score after baseline `W_0`, independent final snapshot, score weights (`20/10/6/4/2/1`), trend, and remediation rules are mandatory.

Never fork the implementing conversation for review or close a local fix before later independent verification. Resolve every feasible authorized finding; a structural flag alone is not a stop, and a repeated signature requires shared-root-cause investigation before another fix. Any score decrease converges (`0 < r_n < 1`); equal scores plateau and increases do not converge. Unavailable independent coverage, stale final evidence, an unmet approval boundary, plateau/non-convergence with open findings, or exhaustion of a stricter caller budget stops the dependent route. Open `security` or `critical` findings forbid completion and commit; unresolved `high` findings require escalation rather than deferral. At a stop, report only completed-round scores (for example `W_0 → W_1 → W_2`), or `not-run` when no review completed, plus per-tier residue, evidence, owner and next action; invoke `AskUserQuestion` only for the concrete missing decision or authorization. A clean loop still requires the owning workflow's remaining gates.

## Confidence Block (required on all analysis tasks)

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md -->

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

**No routine re-read pass.** Current models mostly catch their own errors without a mandated re-score cycle; forcing one anyway compounds token cost without reliably improving results. Write once, full care.

A second pass fires only on a **named, actionable gap** — a source not read, a claim not grounded, a section the ask required and the draft lacks. Address that gap, record it under **Refinements**. Generic phrases ("re-checked, looks fine", "reviewed for completeness") aren't gaps, never justify a pass or a score rise. Cap 2. Report the real score — never inflate; `foundry:calibrate` catches bias.

Gaps that can't be closed (info-access limits, tooling absent) are **documented in the Confidence block, not chased** — caveat and move on.

## Python Code Complexity (when writing or reviewing Python)

Before delivering any Python function or class: cyclomatic complexity ≤12, required (no-default) arguments ≤7, branches ≤12, statements ≤50, return points ≤6. Violation → refactor before delivering. `# noqa: PLR...` / `# noqa: C901` permitted only when refactoring is genuinely impossible (generated code, protocol-mandated signature) — always paired with an inline comment explaining why. Verify: `ruff check --select C901,PLR`.

## Pre-Handover Check (trigger: a named gap the analysis itself cannot close)

Trigger is a **specific unproven claim**, not a score crossing a line: a premise no source was read for, a conclusion resting on one ambiguous signal, an alternative never examined. A low score with every gap already documented needs no dispatch — say so, hand over. When the trigger fires: proof per uncertain claim, re-examine assumptions from first principles. `bridge@borda-ai-rig` available → dispatch `bridge:review` with the exact-args template in `_full/quality-gates.md` §Pre-Handover Check (read it at this trigger; never pass placeholders or a workflow step label), incorporate findings; bridge absent/disabled → state the specific gap so the user can decide to re-run.

## Write-Delegation Checklist (trigger: any `bridge:implement` call)

Before the call read `_full/quality-gates.md` §Write-Delegation Checklist and follow it: complete brief (finding, paths, evidence, permitted edits, result, stop condition, verification command), clean git tree first; after return read the full diff yourself + run the proof command; 2+ fix rounds on same issue → finish by hand; commit only after own diff read + proof run — the bridge never commits.

## Link Verification

**Never add a URL without all four steps, every time — no exemption for domain/protocol/path similarity to an already-verified URL:**

1. **Fetch** — call WebFetch (or equivalent); URL must return non-error (not 4xx/5xx). HTTP 200 is necessary but not sufficient — steps 2 and 3 still mandatory
2. **Read** — read the actual page content; don't rely on URL structure or HTTP status alone
3. **Match** — confirm content matches the intended description; no match = don't add the link
4. **Independent** — every URL needs its own Fetch+Read+Match pass; a verified URL on the same domain doesn't exempt others; skipping any step — including inferring validity from URL structure or HTTP status alone — is a violation

Applies to: agent files, skill files, CLAUDE.md, any markdown.

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

## Prose Compression — Output Files

Applies to all agents; compression tier by destination. Cap is a **soft compression target, not a truncation trigger** — never drop evidence, findings, or CRITICAL/HIGH content to force a file under cap. Compress prose (articles, filler, hedging, verbose framing) first; still over cap after full compression → let it run over rather than lose substance. Structurally large artifacts (multi-agent aggregates, batch reports) legitimately exceed the target — a signal the content warrants its size. Only LOW/Nitpick-severity items are droppable for space; CRITICAL and HIGH always survive intact.

The soft-cap escape isn't licence to run long. Written deliverables skew longer on current models than the caps assume, so match document length to what the task needs: cover the substance, add no filler sections, redundant summaries, restated findings, or boilerplate. Length earned by evidence is fine; length earned by padding is not.

Size estimate: `$(( $(wc -c < file) / 3 ))` tokens — `/4` predates the current tokenizer and under-reports by roughly 30%.

- `.reports/` (human review) — **normal caveman**, ~10K tokens (~500 lines) target: drop articles/filler/hedging; full sentences where clarity demands; fragments OK for terse findings
- `.temp/` (consolidator handover) — **ultra caveman**, ~10K tokens (~500 lines) target: fragments only, zero filler, shortest synonyms, ~30–40% tighter than normal caveman; full evidence coverage still required — compress prose, not substance

## Report File Format

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates.md -->

**Universal terminal-print rule**: when a skill or agent writes a report file whose first non-whitespace line is `---` (YAML metadata block), that block MUST be rendered in the terminal as a **simple two-column Markdown table** (`Field | Value`, one row per YAML key, in file order) — never dumped as raw YAML — as the **first content of the reply**, before the report path, before the executive summary, before anything else. Applies to ALL skills and agents producing such reports, no per-skill restatement needed. The table IS the reply header; omit the `╔═╗` Re:Anchor box when leading with it (see `communication.md` exemption).

Every report file created via output routing begins with this YAML `---`-delimited block; it stays raw YAML on disk (machine-parseable by downstream skills) and is converted to the table only for the terminal print.

**Value length cap — single physical line only**: every field value ≤100 chars, one line, no embedded wrap (a soft-wrapped value breaks GFM table parsing from that row down). Long detail (`Focus`, `Summary`) belongs in the prose executive summary below the table, not the cell.

**Required minimum fields** (all reports): `Title`, `Date`, `Scope`, `Focus`, `Agents`, `Outcome`, `Confidence`, `Next steps`, `Path`. Add skill-specific fields after (e.g. Verdict/CI/Risk/Blockers for `develop:review`). Skills with dedicated output routing (audit, review, resolve, analyse, release) must include an equivalent `---` block at the top of their report files.

_Outcome legend_: `✓` = approved/ready/clean · `⚠` = needs-attention/needs-work · `✗` = blocked/rejected. Distinct from `!`, reserved for standalone alert blocks (`! BREAKING`, `**! BLOCKED**`) — see §Reporting Findings.

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

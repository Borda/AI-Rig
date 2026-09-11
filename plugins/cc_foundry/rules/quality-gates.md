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

## Adversarial Convergence Loop (any review → fix cycle)

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, AGENTS.md -->

Governs every cycle where an independent review produces findings and those findings get fixed: pre-commit review, `/oss:review` → `/oss:resolve`, `/develop:review` → `/develop:fix`, root-cause fix loops, report revision. It decides when to stop, and stopping on a plateau is a result, not a failure.

**Loop**: up to **3** review + fix iterations by default. Each iteration dispatches an independent reviewer — `foundry:challenger` via `Agent()`, or `bridge:review` when the bridge plugin is available. Never `subagent_type: "fork"`: a fork inherits the implementer's reasoning trail and reviews toward confirming it. The reviewer gets diff + spec + symptom, never the implementation narrative.

**Severity weights** — the ladder is the one in `audit/severity-table.md`, with `nit` added at the bottom so trivial style findings cannot mask a stall:

| security | critical | high | medium | low | nit |
| -- | -- | -- | -- | -- | -- |
| 20 | 10 | 6 | 4 | 2 | 1 |

**Score**: `W_n = Σ weight(open findings)` after iteration *n*. `W_0` is the first review's score. **Trend**: `r_n = W_n / W_{n−1}`.

| Condition | Reading | Action |
| -- | -- | -- |
| `W_n == 0` | clean | Done. Proceed. |
| `r_n ≤ 0.5` | converging | Continue if iterations remain. |
| `0.5 < r_n < 1.0` | plateau | Stop. More iterations will not clear it. |
| `r_n ≥ 1.0` | non-converging | Stop immediately. Review finds more than fixes remove — the approach is wrong, not incomplete. |

**Fix scope — targeted only**: a finding is fixed where it is, with the smallest change that closes it. Fixes that contribute to convergence are local: one file, one predicate, one call site. Do not take a finding as licence to restructure the surrounding design.

**Structural findings are flagged, never fixed inside the loop.** A finding is structural when closing it would change a contract rather than an implementation: a script's argument or output shape, a module boundary, a shared file's schema, a skill's step order, or anything that would force edits across files the finding does not name. Such a fix invalidates the review that produced it and reopens the tree to a fresh wave of findings — the iteration budget then measures churn, not progress. On a structural finding: **stop the loop immediately**, do not apply it, report it with its blast radius alongside the current score series, and invoke `AskUserQuestion`. It resumes only on explicit user approval, as its own scoped piece of work. This overrides the trend table: a structural finding stops the loop even at `r_n ≤ 0.5`, and even when it is the only one open.

**Hard blocks, whatever the trend**: any open `security` or `critical` finding means never declare done and never commit. Same finding signature twice running → stop at once, without waiting for the cap (`debugging.md` §Root-Cause Discipline).

**On any stop with `W_n > 0`**: report the score series (`W_0 → W_1 → W_2`) with per-tier counts, name what remains, and invoke `AskUserQuestion`. Never pass a plateau silently.

Worked example, tier-assignment guidance, and why `r ≥ 1.0` stops rather than retries: `_full/quality-gates.md` §Adversarial Convergence Loop.

## Confidence Block (required on all analysis tasks)

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md -->

Every analysis agent **must** end with:

```markdown
## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- [specific limitation]
                          ← blank line required; Refinements is a peer field, not a sub-bullet
**Refinements**: N passes.
- Pass 1: [what gap was addressed — must name the gap, not just say "re-checked"]
```

> **Never skip** — missing Confidence block = rule violation.

- Omit **Refinements** if 0 passes (don't write "0 passes") — omit individual **Gaps** bullets if none, but keep the **Gaps** header
- **Score**, **Gaps**, **Refinements** = peer top-level fields — never nest Refinements under Gaps; blank line before **Refinements** required
- Score < 0.85 → ⚠ on the score line AND on the line immediately after (standalone, not a Gaps bullet): "orchestrator may re-run with the specific gap addressed"
- Gaps = primary signal — surfaces implicit limitations for re-run decisions

## Internal Quality Loop (analysis tasks only)

**No routine re-read pass.** Current models mostly catch their own errors without a mandated re-score cycle; forcing one anyway compounds token cost without reliably improving results. Write once, at full care.

A second pass fires only on a **named, actionable gap** — a source not read, a claim not grounded, a section the ask required and the draft lacks. Address that specific gap, then record it under **Refinements**. Generic phrases ("re-checked, looks fine", "reviewed for completeness") are not gaps and never justify a pass or a score rise. Cap 2. Report the real score — never inflate; `foundry:calibrate` catches bias.

Gaps that cannot be closed (info-access limits, tooling absent) are **documented in the Confidence block, not chased** — caveat and move on.

## Python Code Complexity (when writing or reviewing Python)

Before delivering any Python function or class: cyclomatic complexity ≤12, required (no-default) arguments ≤7, branches ≤12, statements ≤50, return points ≤6. Violation → refactor before delivering. `# noqa: PLR...` / `# noqa: C901` permitted only when refactoring is genuinely impossible (generated code, protocol-mandated signature) — always paired with an inline comment explaining why. Verify: `ruff check --select C901,PLR`.

## Pre-Handover Check (trigger: a named gap the analysis itself cannot close)

Trigger is a **specific unproven claim**, not a score crossing a line: a premise no source was read for, a conclusion resting on one ambiguous signal, an alternative never examined. A low score with every gap already documented needs no dispatch — say so and hand over. When the trigger fires: proof per uncertain claim, re-examine assumptions from first principles. `bridge@borda-ai-rig` available → dispatch `bridge:review` with the exact-args template in `_full/quality-gates.md` §Pre-Handover Check (read it at this trigger; never pass placeholders or a workflow step label), incorporate findings; bridge absent/disabled → state the specific gap so the user can decide to re-run.

## Write-Delegation Checklist (trigger: any `bridge:implement` call)

Before the call read `_full/quality-gates.md` §Write-Delegation Checklist and follow it: complete brief (finding, paths, evidence, permitted edits, result, stop condition, verification command), clean git tree first; after return read the full diff yourself + run the proof command; 2+ fix rounds on same issue → finish by hand; commit only after own diff read + proof run — the bridge never commits.

## Link Verification

**Never add a URL without all three steps, every time — no exemption for domain/protocol/path similarity to an already-verified URL:**

1. **Fetch** — call WebFetch (or equivalent); URL must return non-error (not 4xx/5xx). HTTP 200 is necessary but not sufficient — steps 2 and 3 still mandatory
2. **Read** — read the actual page content; don't rely on URL structure or HTTP status alone
3. **Match** — confirm content matches the intended description; no match = don't add the link

Applies to: agent files, skill files, CLAUDE.md, any markdown.

## Output Routing

**Long output** (multi-item analysis, 5+ findings — including lists of 5+ items: module names, issues, files —, or prose >~10 lines) → two mandatory steps, in order:

1. **Write tool call** — create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` (new file — never overwrite; append a counter suffix if the slug exists, e.g. `-2.md`); full evidence coverage, ultra-caveman compressed (see §Prose Compression — "full" means no dropped findings, not verbose prose). **Execute the Write tool call; do not narrate intent and proceed without calling it** — never skipped; pipeline/background mode only exempts the follow-up gate (step 2.4), not this Write. Distinct from any other file write the task also does.
2. Print to terminal, in this order: (1) **YAML header table** — render the `---` metadata block as a two-column Markdown table (`Field | Value`, one row per key, each value on a single physical line ≤100 chars — a wrapped value loses its leading `|` and breaks GFM table parsing from that row down); never print raw YAML (see §Report File Format). No YAML block → fall back to a plain ASCII verdict line, `·` separator: `verdict: ⚠ NEEDS_WORK · findings: 8 · ...` (verdict word prefixed with its symbol — see §Reporting Findings). (2) **Report path** — `→ <filepath>`. (3) **Executive summary** — 2–3 sentence overview + every critical/high finding listed individually; omit medium/low detail unless ≤2 total findings. (4) **Follow-up gate** — invoke `AskUserQuestion` as the final step; skip only when: spawned via `Agent()`, running inside another skill's pipeline, or the prompt explicitly states background/pipeline mode — when in doubt, invoke.

- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; do **not** create a file
- **Copy-intent override**: output destined for an external artifact (PR body, release notes, report to share) → write to file regardless of length; output read in-context and acted on immediately (audit findings, calibration result, code review) → terminal only even if long
- **Follow-up gate follow-through**: selected option triggers a skill → call `Skill(skill=..., args=...)` in the same turn; never narrate intent as prose ("Invoke that next.") and stop without acting
- **Don't ask what you can't honor**: selected option can't trigger automatic action (`disable-model-invocation: true`, or output is intermediate with a downstream AskUserQuestion coming anyway) → print the suggestion as plain text instead of asking a hollow question

## Prose Compression — Output Files

Applies to all agents; compression tier by destination. Cap is a **soft compression target, not a truncation trigger** — never drop evidence, findings, or CRITICAL/HIGH content to force a file under cap. Compress prose (articles, filler, hedging, verbose framing) first; still over cap after full compression → let it run over rather than lose substance. Structurally large artifacts (multi-agent aggregates, batch reports) legitimately exceed the target — that's a signal the content warrants its size. Only LOW/Nitpick-severity items are droppable for space; CRITICAL and HIGH always survive intact.

The soft-cap escape is not licence to run long. Written deliverables skew longer on current models than the caps assume, so match document length to what the task needs: cover the substance, and add no filler sections, redundant summaries, restated findings, or boilerplate. Length earned by evidence is fine; length earned by padding is not.

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

- **Coverage at the finding stage, filtering downstream**: report every issue found, including low-confidence and low-severity ones; attach confidence and severity so a later stage can rank. Severity words in output-routing rules ("omit medium/low detail") govern the **printed summary**, never what gets investigated or recorded — a finding dropped at discovery cannot be recovered by a filter. Never instruct an agent to "only report high-severity issues" or "be conservative": current models follow that literally, investigating just as deeply and then reporting less
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

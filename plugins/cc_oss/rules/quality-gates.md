---
description: Output quality standards — Confidence block, link verification, output routing
paths:
  - '**'
---

## Adversarial Convergence Loop (any review → fix cycle)

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md (canonical), plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, AGENTS.md -->

Governs every cycle where an independent review produces findings and those findings get fixed — `/oss:review` → `/oss:resolve`, pre-commit review, contributor-PR fix rounds. Decides when to stop; stopping on a plateau is a result, not a failure.

**Loop**: up to **3** review + fix iterations. Each dispatches an independent reviewer — `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` when the bridge plugin is available. Never `subagent_type: "fork"`: a fork inherits the implementer's reasoning trail and reviews toward confirming it. Reviewer gets diff + spec + symptom, never the implementation narrative.

**Severity weights**:

| security | critical | high | medium | low | nit |
| -- | -- | -- | -- | -- | -- |
| 20 | 10 | 6 | 4 | 2 | 1 |

**Score**: `W_n = Σ weight(open findings)` after iteration *n*. **Trend**: `r_n = W_n / W_{n−1}`.

| Condition | Reading | Action |
| -- | -- | -- |
| `W_n == 0` | clean | Done. Proceed. |
| `r_n ≤ 0.5` | converging | Continue if iterations remain. |
| `0.5 < r_n < 1.0` | plateau | Stop. More iterations will not clear it. |
| `r_n ≥ 1.0` | non-converging | Stop immediately — the approach is wrong, not incomplete. |

**Fix scope — targeted only**: fix a finding where it is, with the smallest change that closes it. Converging fixes are local — one file, one predicate, one call site. A finding is not licence to restructure what surrounds it.

**Structural findings are flagged, never fixed inside the loop.** Structural = closing it changes a contract, not an implementation: a script's argument or output shape, a module boundary, a shared file's schema, a skill's step order, or any fix reaching files the finding does not name. Such a fix invalidates the review that produced it and reopens the tree to a fresh wave of findings, so the iteration budget measures churn instead of progress. Stop the loop at once, leave the finding unapplied, report it with its blast radius beside the current score series, and invoke `AskUserQuestion`. It resumes only on explicit user approval, scoped as its own work. Overrides the trend table — a structural finding stops the loop at `r_n ≤ 0.5` and when it is the only one open.

**Hard blocks, whatever the trend**: any open `security` or `critical` finding means never declare done and never commit. Same finding signature twice running → stop at once.

**On any stop with `W_n > 0`**: report the score series (`W_0 → W_1 → W_2`) with per-tier counts, name what remains, invoke `AskUserQuestion`. Never pass a plateau silently.

## Confidence Block (required on all analysis tasks)

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md -->

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

- Omit **Refinements** if 0 passes — omit individual **Gaps** bullets if none, keep **Gaps** header
- **Score**, **Gaps**, **Refinements** = peer top-level fields — never nest Refinements under Gaps; blank line before **Refinements** required
- Score < 0.85 → ⚠ on score line AND next line: "orchestrator may re-run with the specific gap addressed"
- Gaps = primary signal — surfaces implicit limitations for re-run decisions

## Internal Quality Loop (analysis tasks only)

**No routine re-read pass.** Current models mostly catch own errors without mandated re-score cycle; forcing one anyway compounds tokens, doesn't reliably improve. Write once, full care.

1. Second pass fires only on **named, actionable gap** — source not read, claim not grounded, section the ask required and draft lacks
2. Address that gap, record under **Refinements**; generic phrases don't count — never justify pass or score rise
3. Gaps unclosable (info-access limits, tooling absent) → document in Confidence block, not chased
4. Cap 2 passes. Report real score — never inflate

## Pre-Handover Check

Trigger is a **specific unproven claim**, not a score crossing a line: premise no source read for, conclusion resting on one ambiguous signal, alternative never examined. Low score with every gap already documented → no dispatch; say so and hand over. When triggered and `bridge@borda-ai-rig` available → render and call `Skill(skill="bridge:review", args="Read-only adversarial review of <exact area and target paths>. Uncertain claims: <complete claim list>. Current evidence: <source paths or observations>. Challenge each claim, identify missing evidence and alternatives, and return actionable findings with locations; do not apply fixes.")`; never pass the placeholders or a workflow step label. Incorporate findings before handover. If the bridge is absent or disabled → state the gap and score explicitly so the user can decide to re-run.

## Link Verification

**Never add URL without all three steps:**

1. **Fetch** — call WebFetch (or equivalent); URL must return non-error (not 4xx/5xx)
2. **Read** — read actual page content; don't rely on URL structure or HTTP status alone
3. **Match** — confirm content matches intended description; no match = don't add link
4. **Independent** — every URL needs own Fetch+Read+Match pass; verified URL on same domain doesn't exempt others; skipping any step is violation

- Applies to: agent files, skill files, CLAUDE.md, any markdown

## Output Routing

- **Long output** (multi-item analysis, 5+ findings — including lists of 5+ items: module names, issues, files —, or prose >~10 lines) → two mandatory steps in order:

1. Call **Write tool** to create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` where `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` (new file — never overwrite; append counter suffix if slug exists, e.g. `-2.md`); file gets **full content**
2. Print to terminal in order:
   1. **YAML header table** — render `---` metadata block from top of report file as simple two-column Markdown table (`Field | Value`, one row per key, each value single physical line ≤100 chars — never wrap a value inside a cell: wrapped continuation line loses leading `|`, breaks GFM table parsing from that row down) — never print raw YAML verbatim (see **Report File Format** below); if skill has no YAML block in file, fall back to plain ASCII verdict line; no Unicode box-drawing chars (`─`, `═`, `│`, `┌` etc.); use `·` as separator: `verdict: ⚠ NEEDS_WORK · findings: 8 · critical: 0 · high: 2 · medium: 4 · low: 2 · confidence: 0.88` (verdict word prefixed with its symbol — see §Reporting Findings)
   2. **Report path** — `→ <filepath>`
   3. **Executive summary** — prose: 2–3 sentence overview + each critical/high finding listed; omit medium/low detail unless ≤2 total findings
   4. **Follow-up gate** — invoke `AskUserQuestion` as final step; skip when background agent or inside another skill's pipeline

- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; do **not** create file
- Prose paragraphs: no hard line breaks at column width
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. Canonical examples:
  - `oss:review N` → (a) `/oss:resolve N` · (b) `/oss:resolve report` · (c) `/oss:resolve N report` · (d) walk findings · (e) skip
  - `oss:analyse N` → (a) `/develop:fix` · (b) `/develop:feature` · (c) `/oss:review N` · (d) draft reply · (e) skip
- **Follow-up gate follow-through**: when `AskUserQuestion` returns with skill-invocation option — call `Skill(skill=..., args=...)` same turn; never narrate intent as prose and stop without acting

## Report File Format

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates.md -->

Every report file from output routing must begin with YAML metadata block between `---` delimiter lines. Block = canonical meta summary — file keeps raw YAML (machine-parseable by downstream skills); when printed to terminal, convert to two-column table (`Field | Value`, one row per key, file order) before executive summary — never raw YAML in terminal. Table IS reply header; omit `╔═╗` Re:Anchor box when leading with it (see `communication.md` exemption).

**Value cap — single line only**: each value ≤100 chars, one physical line, no wrap. Wrapped cell loses leading `|` on continuation line → parser drops table from that row down. Long detail (Focus, Summary) → short label in cell, full text in prose exec summary below.

**Required minimum fields** (all reports):

```yaml
---
Title:      [Skill] — [subject]
Date:       [YYYY-MM-DD]
Scope:      [what was analyzed — file paths, PR#, repo, run-id, etc.]
Focus:      [aspect examined — e.g. "code review", "repo vitality", "thread triage"]
Agents:     [agent names that contributed — comma-separated]
Outcome:    [verdict — ✓ APPROVE | ⚠ NEEDS_WORK | ✗ BLOCKED | etc.]
Confidence: [score] — [key gaps]
Next steps: [recommended follow-up skill invocation]
Path:       → .reports/<skill>/<timestamp>/<name>.md
---
```

After required fields, add **skill-specific fields** for report type (e.g. PR, PR Type, CI, Summary for `oss:review`; Health Score, Axes for `oss:analyse` vitality). All oss skills with dedicated output routing — `oss:review`, `oss:analyse`, `oss:resolve`, `oss:release` — must include equivalent `---` block at top of report files.

## Reporting Findings

- **Coverage at finding stage, filtering downstream**: report every issue found, including low-confidence and low-severity; attach confidence + severity so later stage ranks. Severity words in output-routing ("omit medium/low detail") govern **printed summary** only — never what gets investigated or recorded; finding dropped at discovery not recoverable by filter. Never instruct agent to "only report high-severity issues" or "be conservative" — current models follow literally, investigate just as deep, report less
- **Report before fixing**: state every finding before any fix — never silently mutate
- **Per-fix narration**: before each file edit or tool call, state what changes and why
- **! BREAKING format**: breaking findings = standalone block — never inline or buried in table row:

```text
! BREAKING — <one-line impact: what breaks and who is affected>
Fix: <concrete action to resolve>
```

- Severity/verdict markers: `!` = critical (standalone alert-block prefix only, e.g. `! BREAKING`) · `✗` = blocked/rejected · `⚠` = warnings/needs-attention · `✓` = pass/approved · hint = fix hint — prefix the verdict word wherever printed; `!` never appears as a table-cell symbol, only as the alert-block prefix
- Terminal colors: RED = critical · YELLOW = warnings · GREEN = pass · CYAN = fix hint

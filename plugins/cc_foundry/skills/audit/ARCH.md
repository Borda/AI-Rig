<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/foundry:audit` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md` and `modes/*.md` are the only normative sources. It exists so the shape — what fans out, what joins, what blocks — can be read without walking a 500-line skill plus five mode files.
>
> **Keep it current.** Any change to block order, gate placement, fan-out width or what runs beside what lands here in the same commit. Every schema block carries a `(step N)` tag naming its step; renumbering a step updates the tag and the index at the end. A schema that disagrees with `SKILL.md` is worse than none — `SKILL.md` wins every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

## Schema

```
SETUP  (pre-flight, steps 1, 1b, 1c, 2)
  flags · LOCAL_MODE  (pre-flight)
  ◆ unknown flag?  (pre-flight)                       [conditional]
  pre-commit (4h cache) (step 1) · Layer-1 static pass (step 1b,
    authoritative) · churn (step 1c)
  collect inventory (Glob · plugin-layout resolve · coverage check) (step 2)
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
PER-FILE AUDIT  (step 3)           SYSTEM-WIDE CHECKS  (step 4)
  ▣ curator                          full sweep: ▣ curator ×5, one per
  batches of EFFECTIVE_BATCH           scope group (agents · skills ·
  files, cap CAP_OPUS                  shared · setup · security)
                                     scoped run: native tools/bash inline
                                     ▣ web-explorer — docs freshness
                                      ◆ ! BREAKING finding?  [conditional]
  |                                                  |
  +--------------------- JOIN -----------------------+
  |
AGGREGATE  (step 5)  ▣ curator consolidator
  classify by severity → aggregate.md, summary.jsonl
  |
LOW-CONF REMEDIATION  (step 5b)  FAN 3   [conditional, any slug scores <0.80]
  ▣ curator re-run           double-reasoning pass, targets prior gaps
  ▣ web-explorer docs-check  verify findings against current schema
  Codex review                adversarial pass (bridge, outside both pools)
  JOIN → ▣ curator mini-consolidator merges the three into aggregate.md
  |
CROSS-VALIDATE CRITICAL  (step 6)  ▣ curator × ≤3  [conditional, critical >0]
  |
◆ FOLLOW-UP GATE  (step 7)  fix option (a–d), always fires unless --skip-gate
  |                            (fix option picked only, below)
FIX DISPATCH  (step 8)
  ◆ Fix-ALL category decisions, ≤4 calls    [conditional, option (c) only]
  ▣ challenger ‖ ▣ curator — adversarial pre-apply gate, per finding
  Phase 1  ▣ parallel-safe fixes, one agent per file, single response
  Phase 2  ▣ curator mini-agent re-reads → sequential dependency-ordered
  |
CODEX CROSS-CHECK  (step 9)    [conditional: bridge, >1 file changed]
  |
RE-AUDIT  (step 10)  ▣ curator per changed file
  new fixable findings → loop to FIX DISPATCH, 5-pass hard limit
  |
FINAL REPORT  (step 11)  Write $RUN_DIR/report.md + terminal print  [not a gate]
```

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| PER-FILE AUDIT ‖ SYSTEM-WIDE CHECKS | 2 | fixed — curator batches (opus) + web-explorer (sonnet) are separate pools | AGGREGATE |
| PER-FILE AUDIT's own batches | `EFFECTIVE_BATCH = max(BATCH_SIZE_MIN, ceil(total/MAX_BATCHES))` | `CAP_OPUS=5`; `--fast` widens toward it | JOIN |
| SYSTEM-WIDE CHECKS's own lanes | 5 on a full sweep (agents · skills · shared · setup · security), 0 on a scoped run | fixed by scope group, not tunable; opus pool | JOIN |
| LOW-CONF REMEDIATION passes | 3 per slug (1 consolidated batch instead when >8 slugs score low) | opus (curator) + sonnet (web-explorer) pools; Codex bridge outside both | mini-consolidator merge |
| CROSS-VALIDATE CRITICAL | ≤3 verifiers, batches of ≤2 findings each | opus pool, shared with PER-FILE AUDIT/AGGREGATE/REMEDIATION | FOLLOW-UP GATE |
| FIX DISPATCH adversarial gate | 2 per finding (challenger + curator) | opus pool | fix-agent dispatch decision |
| FIX DISPATCH Phase 1 | 1 per parallel-safe file — `foundry:curator` for `.md`, `foundry:sw-engineer` for code, all issued in one response | opus pool | Phase 2 |
| RE-AUDIT | 1 per changed file | opus pool | convergence check, or loop to FIX DISPATCH |

Per-mode fan widths (Adversarial, Efficiency, Upgrade phases) sit in their own tables under **Mode branches** below, since they only exist when that flag is passed.

**What bounds the top-level fan.** PER-FILE AUDIT and SYSTEM-WIDE CHECKS read the same Step 2 inventory but write disjoint outputs (`<slug>.md` files vs `system-checks-<scope>.md`, one per scope group) with no dependency between them — `SKILL.md` hard-mandates launching both in the same response, never Step 3 then Step 4.

**Shared pool discipline.** Every `foundry:curator`, `foundry:challenger`, and `foundry:sw-engineer` spawn across PER-FILE AUDIT, AGGREGATE, LOW-CONF REMEDIATION, CROSS-VALIDATE CRITICAL, FIX DISPATCH, and RE-AUDIT draws from one shared `CAP_OPUS=5` pool — before launching, sum every opus-tier batch due in the same response across whichever phases are active and wave in `WAVE_STEP=5` increments if the sum exceeds the cap. `foundry:web-explorer` and `foundry:qa-specialist` draw a separate `CAP_SONNET=8` pool and may run concurrently with the opus wave. Codex bridge calls are neither `Agent()` spawns nor pooled.

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| unknown `--flag` | conditional | SETUP |
| `! BREAKING` finding acknowledgment | conditional, batched ≤4/call | the check that raised it |
| **FOLLOW-UP GATE** | **always** (unless `--skip-gate`) | FIX DISPATCH, or → FINAL REPORT |
| Fix-ALL NON_AUTO_FIXABLE category decisions | conditional, option (c) only, ≤4 calls | FIX DISPATCH fix pass |

Clean scoped run with no breaking findings and no fix pick costs exactly 1 `AskUserQuestion` call (FOLLOW-UP GATE). Picking "Fix ALL" adds up to 4 more. Breaking findings add `ceil(N/4)` more, independent of the rest.

## Mode branches

`--upgrade`, `--adversarial`, `--efficiency` are mutually exclusive with `--upgrade` (not with each other). Each replaces or stacks onto the SETUP → FOLLOW-UP GATE span above; all three rejoin the same FOLLOW-UP GATE once their own findings exist, then FIX DISPATCH → CODEX CROSS-CHECK → RE-AUDIT proceed identically.

```
ADVERSARIAL  (adversarial.md)  FAN 4   runs alongside PER-FILE AUDIT by default
  ▣ curator challenger  (phase A)          batches of 2 — NOT-for gaps, contradictions
  ▣ curator unconstrained  (phase A-prime) batches of 2 — beyond-checklist judgment
  Codex bridge  (phase B)                  cross-file inconsistencies (no Agent() pool)
  ▣ qa-specialist  (phase D)               per plugin with bin/ — OWASP Top 10
  JOIN → ▣ curator consolidator  (phase C)
    dedup vs same-run summary.jsonl → FOLLOW-UP GATE
```

| Phase | Width | Bound by | Joins at |
| -- | -- | -- | -- |
| A challenger / A-prime unconstrained | batches of 2 files (`ADVERSARIAL_BATCH_SIZE`) | opus pool, shared with the main table's rows | Phase C |
| D security review | `BATCH_SIZE_MIN=5` bin/ scripts per plugin | sonnet pool, shared with docs-freshness | Phase C |
| C consolidator | 1 | opus pool | FOLLOW-UP GATE |

Alone (no prior audit in RUN_DIR) skips PER-FILE AUDIT through CROSS-VALIDATE CRITICAL entirely and reports unfiltered.

```
EFFICIENCY  (efficiency.md)  FAN 3   replaces PER-FILE AUDIT..CROSS-VALIDATE CRITICAL
  ▣ curator per file  (phase A)      model tier · effort · bloat · E8/E9/E10
  bash scan  (phase B)               spawn-pattern + duplication (no Agent())
  ▣ curator per plugin  (phase B2)   Check 33 code-block clusters
  JOIN → ▣ curator consolidator  (phase C)
    cost-reduction report → FOLLOW-UP GATE
```

| Phase | Width | Bound by | Joins at |
| -- | -- | -- | -- |
| A per-file sweep | `EFFECTIVE_BATCH` (recomputed — Step 3 skipped) | opus pool | Phase C |
| B2 code-block grouping | 1 per plugin | opus pool | Phase C |
| C consolidator | 1 | opus pool | FOLLOW-UP GATE |

Skips PER-FILE AUDIT through CROSS-VALIDATE CRITICAL outright — never runs the standard per-file quality audit. `--adversarial --efficiency` combined: both run, each to its own `$RUN_DIR` subdir (`adversarial/`, `efficiency/`); FOLLOW-UP GATE fires once, merged counts.

```
UPGRADE  (upgrade.md)            entirely sequential, no fan-out
  Phase 1  gate check — abort on open critical/high
  |
  Phase 2  ▣ web-explorer — docs + RTK check
  |
  Phase 3  apply config proposals (inline Edit, sequential)
  |
  Phase 4  ▣ general-purpose × 2 per proposal, sequential
           (baseline calibrate → edit → post calibrate → accept/revert)
           max 3 proposals
  |
  Phase 5  report
  |
◆ FOLLOW-UP GATE  (step 7 style)  per SKILL.md "## Follow-up gate"
```

| Phase | Width | Bound by | Joins at |
| -- | -- | -- | -- |
| Phase 2 docs+RTK | 1 | sonnet pool | Phase 3 |
| Phase 4 A/B test | sequential, 2 spawns per proposal, ≤3 proposals | not pooled — sequential by design | Phase 5 |

Entirely sequential — no PER-FILE AUDIT fan-out, no FIX DISPATCH/RE-AUDIT loop; ends by firing the same follow-up gate, per `SKILL.md`'s "## Follow-up gate" note.

## Degenerate cases

- `plugin` / `setup` / tier-3 single agent-or-skill scope — PER-FILE AUDIT collapses to one `foundry:curator` spawn; SYSTEM-WIDE CHECKS runs a narrowed check list.
- Zero CRITICAL findings — CROSS-VALIDATE CRITICAL skipped entirely, no verifier spawned.
- No slug scores below 0.80 — LOW-CONF REMEDIATION skipped entirely.
- No fix option picked at FOLLOW-UP GATE — FIX DISPATCH never starts; flow goes straight to FINAL REPORT.
- `--skip-gate` — FOLLOW-UP GATE suppressed; same effect as declining every option, FIX DISPATCH unreachable.
- CODEX CROSS-CHECK — skipped when FIX DISPATCH touched only 1 file, or when the Codex bridge is unavailable.
- RE-AUDIT convergence loop — exits after pass 1 whenever zero new fixable findings surface; never re-enters FIX DISPATCH past 5 total passes regardless of remaining findings.
- `--upgrade` — the most degenerate mode: no PER-FILE AUDIT fan-out at all, no FIX DISPATCH/RE-AUDIT loop; Phase 4's A/B test is sequential per proposal, never parallel.

## Where this lives in `SKILL.md`

Index of the schema tags above, one row per block. A bare `step N` is a `SKILL.md` step (steps 4–5b and 7 live in `modes/steps-4-5-7.md`, steps 8–10 in `modes/fix.md`); mode files use their own phase labels (`phase A`).

| Block | Steps |
| -- | -- |
| SETUP | Pre-flight, 1, 1b, 1c, 2 |
| PER-FILE AUDIT | 3 |
| SYSTEM-WIDE CHECKS | 4 |
| AGGREGATE | 5 |
| LOW-CONF REMEDIATION | 5b |
| CROSS-VALIDATE CRITICAL | 6 |
| FOLLOW-UP GATE | 7 |
| FIX DISPATCH | 8 |
| CODEX CROSS-CHECK | 9 |
| RE-AUDIT | 10 |
| FINAL REPORT | 11 |
| ADVERSARIAL mode | `modes/adversarial.md` (Phases A–D) |
| EFFICIENCY mode | `modes/efficiency.md` (Phases A–C) |
| UPGRADE mode | `modes/upgrade.md` (Phases 1–5) |

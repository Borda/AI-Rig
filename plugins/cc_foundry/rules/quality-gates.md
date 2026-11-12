---
description: Output quality standards — Confidence block, link verification stub, output routing
paths:
  - '**'
---

> Extracted sections bind before their activity. Resolve with active Foundry’s `bin/resolve_plugin_root.py --plugin-name foundry` helper (registry preferred); set `FOUNDRY_ROOT` to the result in the same Bash call. Never select a different installed version by mtime. For repository maintenance only, use source-tree `plugins/cc_foundry`. Unreadable detail → stop and report.
>
> ```bash
> RULE_FULL="$FOUNDRY_ROOT/rules/_full/quality-gates.md"  # timeout: 5000
> cat "$RULE_FULL"  # timeout: 5000
> ```

## Evidence Grounding (universal)

**Never generate without grounding in evidence** — every claim, finding, URL, or fact: read source, run command, check file first. No hypothesis as fact, no URL unverified, no finding unread. "Obvious"/"well-known"/session recall/training knowledge are **never** evidence — current disk state beats all of them. Evidence inaccessible → state `unable to verify: [reason]` explicitly, never substitute recall or inference.

**Design premises gate at entry, not delivery** — any assumption, constraint claim, or recalled fact used as a pillar of a design/implementation decision must be grounded in evidence read now, when it first enters the design. "Where is this documented?" first — no answer = unverified = no design built on it.

**Evidence tiers**: Tier 1 (official docs, source code read from disk, release notes, spec/RFC, this-session test output) — sufficient alone. Tier 2 (blog posts, tutorials, forums, training knowledge) — needs ≥3 genuinely independent sources OR a confirming experiment; independence + citation-tracing rules in `_full/quality-gates.md` §Evidence Grounding (read before counting Tier 2 sources).

## Adversarial Pass (all generation)

While producing output — not only after — ask "what would make this wrong?". Code: trace the failure path, not just the happy path (off-by-one, stale API, the edge case real data actually hits). Claims: separate "verified" from "pattern-matched" — verify or hedge the latter explicitly.

## Evidence and real-case acceptance

> Before applying, Read `_full/quality-gates.md` §Evidence and real-case acceptance; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Proportional Execution

> Before planning or implementation, including small parent-owned tasks, resolve + Read `_full/quality-gates.md` §Proportional Execution; its full policy binds as if inline.

Validate bounded outcome; decompose if needed. Detail active piece/context; work depth-first through verified closure. Delegate ready disjoint siblings. Plan → challenge → approve within existing authority → delegate/parent execution → parent consolidate. Reuse accepted leaf plans; no repeated formal ceremony or new routine approval.

## Adversarial Convergence Loop (explicitly invoked)

A request to repeat review and fix until a reviewer reports no findings or no security findings, or to resolve all findings; a second independent review round about to be dispatched for the same change; or a skill workflow repeating its review then fix cycle counts as an explicitly invoked loop from that moment: before the next reviewer dispatch, start ledger; follow `_full/adversarial-loop.md` (helper output, stop/plateau rules, user's round budget).

<!-- policy-sibling: plugins/cc_oss/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, AGENTS.md -->

An explicitly invoked iterative convergence workflow retains all its existing rules. Read `_full/adversarial-loop.md` before that loop: its scope, evidence ledger, strictly decreasing nonnegative integer score after baseline `W_0`, independent final snapshot, score weights (`20/10/6/4/2/1`), trend, and remediation rules are mandatory. Before dispatching a challenger round, read `_full/quality-gates.md` §Challenger Rounds.

For the explicitly invoked loop, never fork the implementing conversation for review or close a local fix before later independent verification. Resolve every feasible authorized finding; a structural flag alone is not a stop, and a repeated invariant (same signature or same invariant evidence) requires shared-root-cause investigation before another fix. Any score decrease converges (`0 < r_n < 1`); equal scores plateau and increases do not converge. Unavailable independent coverage, stale final evidence, an unmet approval boundary, plateau/non-convergence with open findings, or exhaustion of a stricter caller budget stops the dependent route. Open `security` or `critical` findings forbid completion and commit; unresolved `high` findings require escalation rather than deferral. At a stop, report only completed-round scores (for example `W_0 → W_1 → W_2`), or `not-run` when no review completed, plus per-tier residue, evidence, owner and next action; invoke `AskUserQuestion` only for the concrete missing decision or authorization. A clean loop still requires the owning workflow's remaining gates.

After each validated round, print the progress table exactly: `| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |`, all rows kept, each cell `old + new`; never reshape or reduce it. Generate it only with the ledger helper and show its legend and reconciliation lines; hand-built tables are invalid, and pre-ledger reviews are disclosed only in prose under the full rule.

## Confidence Block (required on all analysis tasks)

> Before applying, Read `_full/quality-gates.md` §Confidence Block (required on all analysis tasks); binding inline. If it cannot be read, stop and report the missing prerequisite.

## Internal Quality Loop (analysis tasks only)

> Before applying, Read `_full/quality-gates.md` §Internal Quality Loop (analysis tasks only); binding inline. If it cannot be read, stop and report the missing prerequisite.

## Python Code Complexity (when writing or reviewing Python)

Limits per function: cyclomatic ≤12 · required args ≤7 · branches ≤12 · statements ≤50 · returns ≤6 — refactor before delivering. Full rule (noqa exception, verify command): `python-code.md` §Complexity Thresholds, loaded with any `.py` file.

## Pre-Handover Check (trigger: a named gap the analysis itself cannot close)

Trigger: a **specific unproven claim**. Read and follow `_full/quality-gates.md` §Pre-Handover Check before further review; it owns evidence checks, user decisions, dispatch, stop conditions and disclosure.

## Write-Delegation Checklist (trigger: any `bridge:implement` call)

Before the call read `_full/quality-gates.md` §Write-Delegation Checklist and follow it: complete brief (finding, paths, evidence, permitted edits, result, stop condition, verification command), clean git tree first unless the invoking skill explicitly documents a combined-change exception with bounded owned paths, a recorded starting snapshot, combined-diff review and proof commands; only that invocation may use the exception. After return read the full diff yourself + run the proof command; 2+ fix rounds on same issue → finish by hand; commit only after own diff read + proof run — the bridge never commits.

## Link Verification

<!-- policy-sibling: plugins/cc_foundry/rules/markdown.md (§Link Verification) -->

Never add a URL to any file without all four steps, each URL on its own; full wording: `markdown.md` §Link Verification, loaded with any `.md` file.

1. **Fetch** — non-error status; status alone never suffices
2. **Read** — the actual page content
3. **Match** — content matches the intended description, else no link
4. **Independent** — own pass per URL; a verified URL on the same domain exempts nothing

## Output Routing

> Before applying, Read `_full/quality-gates.md` §Output Routing; binding inline. If it cannot be read, stop and report the missing prerequisite.

Conditional follow-up: invoke `AskUserQuestion` only when a required decision or authorization is missing; output length never triggers a question. Continue an already-authorized next action in the same turn. This overrides generic skill completion prompts such as `NEVER SKIP` or `Always fires`. Preserve concrete gates for a new fix scope, paid execution, sensitive or destructive actions, remote changes, and other ungranted authority; existing authorization applies only within its granted scope. Background/pipeline work returns missing decisions to its owner. Never invent a decision or ask what cannot be honored.

## Prose Compression — Output Files

> Before applying, Read `_full/quality-gates.md` §Prose Compression — Output Files; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Report File Format

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates.md, plugins/cc_foundry/rules/_full/quality-gates.md -->

> Before applying, Read `_full/quality-gates.md` §Report File Format; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Reporting Findings

> Before applying, Read `_full/quality-gates.md` §Reporting Findings; binding inline. If it cannot be read, stop and report the missing prerequisite.

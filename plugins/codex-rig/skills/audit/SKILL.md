---
name: audit
description: Audit Codex configuration and workflow contracts, including failures between individually successful steps, plus prompt-efficiency drift; emit evidence-backed gaps and measurable gates.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md). Waits, plan updates, and test runs follow [Agent Waits](../../shared/native-skill-contract.md#agent-waits), [Plan Updates](../../shared/native-skill-contract.md#plan-updates), and [Sandboxed Test Runs](../../shared/native-skill-contract.md#sandboxed-test-runs).

# Audit

> Task-size routing follows [Proportional Execution](../../shared/native-skill-contract.md#proportional-execution); this skill's specialized lifecycle and acceptance checks remain authoritative.

Audit configuration and workflows iteratively: map the system, investigate specific uncertainties, and reassess findings in context. Keep each cycle bounded to the authorized scope. The numbered lifecycle organizes artifacts and gates; analysis revisits affected flows when evidence changes their context.

> Deliver ranked findings and concrete remediation proposals without requiring a fix-selection answer. Apply any already supplied remediation preferences to the recommendations; `skip_gate` remains a compatible legacy input and never adds a selection checkpoint. Audit stays read-only. Ask only for genuinely missing scope, evidence or a consequential decision needed to assess the target; proposing fixes does not authorize implementation.

## Input Schema

```json
{
  "scope": "config|skills|roles|all",
  "target": "optional path",
  "mode": "upgrade|adversarial",
  "axis": "standard|value-per-token",
  "min_cost_reduction": "optional fraction; default 0.05 for value-per-token acceptance",
  "skip_gate": false,
  "done_when": "drift and broken references are ranked with gate result, evidence-backed remediation proposals and explicit coverage limits"
}
```

## Workflow (Exact Commands)

### 01: Create run directory

Run `create_run.py --skill audit` per `../../shared/helper-cli-contract.md`.

### 02: Normalize scope and collect inventory

Scopes:

- `config`: project `.codex/config.toml`, `AGENTS.md` layers, permissions, and routing.
- `skills`: repository/user-authored `.agents/skills/**` plus declared calibration coverage.
- `roles`: role-routing instructions and explicitly supplied plugin/package role-card root.
- `all`: every applicable surface above. Missing optional local skills or roles is `not-configured`, not drift.

Run `rg --files` with the `AGENTS.md`, `.codex/config.toml`, `.agents/skills/**` globs as argv. When `target` is supplied and exists, enumerate regular files under it to depth four with platform-native filesystem walk. Sort and deduplicate both result sets into `<run-directory>/inventory.txt`; record unavailable inputs or collection failures.

### 03: Build an audit ledger before running gates

Write `<run-directory>/audit-ledger.md` with these sections:

- `Inventory`: configured/present policy, skills, and role-routing surfaces.
- `Broken References`: missing files, stale paths, unresolved shared resources.
- `Runtime Leaks`: non-native runner fields/external runtime assumptions.
- `Coverage`: calibration benchmark/behavior.
- `Overlap`: duplicate/fuzzy ownership decisions.
- `Prompt Efficiency`: instruction cost, loaded context, obligation preservation, and value-guard evidence.
- `Recommendations`: ranked fixes.

### Explore workflow outcomes before checking conformance

For a workflow-bearing target, follow its actual entrypoint through helpers, state changes, handoffs, the next ordinary user action. Read the relevant callers and consumers even when unchanged. Start from the user's intended outcome; existing instructions, accepted design decisions, green tests are evidence to question, not proof that the contract is sufficient. Keep the exploration bounded to reachable behavior, explicitly record excluded paths. For a static target with no workflow, record why this check is not applicable.

Write `<run-directory>/workflow-exploration.md` with `Transitions`, `Counterexamples`, `Coverage` sections, including an explicit reason in each section when no workflow applies. Keep the map, focused probes and reconciliations in these existing sections; no additional graph or report format is required.

#### Map the system

Before deep inspection, start `Transitions` with a compact map of the material user flows: intended outcomes, supported entrypoints and actors, producers and consumers, dependencies and shared state, and scope boundaries. Give flows stable names so findings and coverage can refer back to them. Derive connections from source or observed behavior; label assumed or unavailable links. Group by behavior rather than file layout, including interactions between flows and the next ordinary user action. For a static target, map its effective configuration/instruction dependencies without inventing runtime flows.

Choose the next detail by impact on the user's outcome and strength of existing evidence: prioritize an unproven shared dependency, handoff or contradictory assumption over convenient isolated cleanup. The map bounds the audit; it does not claim exhaustive discovery.

#### Investigate one material uncertainty

Trace the selected flow through its relevant implementations and consumers. Use a compact transition table: `Step / actor | Required precondition | Observed postcondition | Next consumer / user action | Evidence / gap`. For each material handoff ask:

- What does the producer actually guarantee, what stronger property does the consumer assume? Distinguish matching content or value from identity, ownership, authority, lifetime, and destination.
- Can every local check succeed while the end result is wrong, temporary, unrecoverable, or misleading to the user? What retains the result after the next normal action?
- What happens when the same flow starts from another currently supported state, resumes, retries, or encounters a change between verification and use? Check those states that the implementation or documented use makes reachable; avoid invented risk matrices.
- What observable example would disprove the claimed safety or completion? Does the existing test assert the user's outcome or merely repeat the implementation's chosen operation?
- For each reached failure, pause, retry or approval gate, what concrete invalid result does it prevent? Test a semantically equivalent valid input as well as that invalid case. Existing policy, exact wording, minimum prose length and green conformance tests are not justification. Classify the check by its effect on structure, factual evidence or actual authority under [Acceptance and recovery](../../shared/native-skill-contract.md#acceptance-and-recovery).

Record gate necessity and the valid/invalid controls in the existing `Counterexamples` section. Prefer deleting an unjustified stop or correcting interpretation at its owning boundary over adding another exception, retry or approval stage. Complementary presentation and generated bookkeeping should recover deterministically within existing authority, preserving original evidence; add a regression through the next consumer. A real evidence gap or unavailable permission remains explicit. Account for stops that were inspected, untested or excluded in `Coverage`; do not claim every workflow is lean from a narrow passing check.

Probe the highest-impact unproven assumption with the smallest safe executable check or source-backed counterexample. Retain its inputs, expected outcome, actual result, rejected alternative; proposed/unavailable probes remain gaps. Include a positive case to distinguish a missing guarantee from a deliberately valid alternative. Audit remains read-only: parent-owned disposable probes may write scratch data within existing authorization; production edits and external actions keep their own scope and permission boundaries.

#### Reassess the finding in context

After each focused investigation, recheck the conclusion against the mapped user outcome, ordinary consumer and affected sibling flows. Record what changed in `Counterexamples`: the hypothesis, evidence, conclusion, affected flow names and next inspection or stop reason. A discovered dependency, reachable state, shared cause, contradiction or invalidated assumption triggers renewed inspection of the affected context. Update priorities and mark earlier conclusions that relied on the invalidated assumption as unverified until rechecked. Source changes invalidate affected evidence; retain its history and bind fresh conclusions to the current source. Follow only evidence-backed dependencies within the authorized scope; record an out-of-scope dependency and the missing decision instead of silently expanding the task.

Repeat with the next material uncertainty from the updated map. Do not use a fixed pass count or keep searching until no new findings appear. Existing recurrence and reasoning-progress stop rules still apply; a blocked probe remains a gap while independent authorized checks continue.

#### Final reconciliation

Before gates and again before classifying the final findings, reconcile every mapped flow and material dependency with `Coverage`: inspected with evidence, unresolved with owner/next check, excluded with scope reason, or inapplicable with evidence. Split partially inspected flows so a local passing check cannot imply full coverage. Tie findings to affected outcomes and reconcile contradictions between local detail and system behavior. Reopen affected claims when later gates reveal source drift or new evidence. Finish when the authorized scope is accounted for and each conclusion is supported or explicitly bounded by a gap; a completed assessment can retain gaps, but cannot certify untested workflows as safe.

Report tested transitions separately from untested coverage. A passing inventory, schema validator, command, or existing regression suite cannot close an untested end-to-end claim. Generalize demonstrated mechanisms into recommendations; keep incident-specific names and benchmark answers out of shipped instructions.

### Instruction composition and productivity regressions

Review effective instruction composition, not only each file separately: user overrides, global managed and unmanaged blocks, project siblings, installed skill names, role cards, runtime defaults, and historical evidence consumed by launchers. Distinguish source readiness from active installation. Use `scripts/install_global_agents.py --check --source <installed-package>/assets/AGENTS.md --codex-home <home>` for read-only global-state diagnosis during explicit setup/audit; never add a per-task setup gate or automatically delete custom instructions.

Use the map to select applicable positive and negative outcome probes for a small parent-only change, authorized serial fallback without redundant consent, productive multi-step investigation, useful unfinished recovery, a feasible structural fix, gradual review-score improvement, and GPT 6+ launch selection. Record why other listed cases are outside the audited composition instead of running unrelated workflows. Where delegation or runtime permissions apply, confirm actual delegated-write and runtime permission denials still reject. A test that only checks prescribed wording or reproduces an implementation choice cannot establish workflow quality. Record each tested invariant, the check that would fail on its counterexample, and residual untested behavior. Review added obligations for current necessity; remove superseded active rules instead of layering another exception over them. Preserve explicitly labeled historical records without treating them as executable routing authority.

### 04: Audit prompt efficiency without using length as quality

Always write `<run-directory>/prompt-efficiency.md` with `Measurement`, `Cost Baseline`, `Loaded Context`, `Obligation Map`, `Value Guards`, `Adversarial Review`, `Recommendations` sections. For `scope=config|roles` with no skill target, record `not-applicable` and why. For `scope=skills|all` or `axis=value-per-token`, audit each discovered local skill root independently; absent optional Codex Rig, Codemap, or Bridge root is `not-configured`, never cross-plugin dependency or failure.

Measure with matched provider-native token counts; else local `tiktoken` with `o200k_base`; else deterministic UTF-8 bytes and words. Never install tokenizer or use network access. Counts are cost evidence, never quality evidence; proxy-only comparison cannot accept candidate, remains `insufficient-evidence`.

For each baseline/candidate pair, use same file set and record hashes, measurement source, static cost, loaded referenced instructions, conditional-load decisions, cost of every reference required by exercised path. A moved instruction is not saving when same run must load it. Map every baseline obligation to its candidate location and evidence, covering safety, ordering, user approval, output/schema, fail-fast, tool-permission, quality-gate requirements.

Value Guards must record exact package/tests, behavioral and calibration results, contract-marker coverage, tool/check failures, completion quality. A live comparison must use paired tasks with same model, effort, task contract, and prompt identity; record native token/cost fields and confidence limits. Treat missing paired live evidence as `insufficient-evidence` for material behavior claim.

Any candidate that removes, moves, or condenses obligations requires adversarial review of obligation map. Before accepting, read [Audit Optimization Evidence](../../shared/audit-cost-contract.md) and retain its schema-2 evidence records. Accept only when all hard guards pass, no critical behavior regresses, tool/check failures don't increase, normalized cost falls by `min_cost_reduction`; otherwise reject or mark insufficient evidence. A shorter candidate fails when it loses obligation, weakens guard, hides loaded-reference cost, or lacks required evidence. Value-per-token scores may rank already accepted candidates but never override hard gates.

A successful assessed audit requires an executed passing `review` gate; all-skipped diagnostic accounting cannot certify assessment. Nonaccepted optimization states remain valid when their limits are explicit.

### 05: Route specialists only when triggered

For `scope=all`, `mode=adversarial`, audits crossing skills, agents, CI/config, or material prompt compression, read, apply `../../shared/specialist-orchestration.md`; never load it for narrow single-surface audit. Write `<run-directory>/specialist-audit-plan.md` packs for:

- `curator`: skill/agent/config drift, duplication, calibration hygiene.
- `linting-expert`: Markdown, Python, shell, ruff/mypy/pre-commit references.
- `cicd-steward`: CI harness, workflow permissions, artifact behavior.
- `challenger`: adversarial check of no-finding or low-risk conclusions.

Stay single-agent for narrow `scope=config`, `scope=skills`, or `scope=roles` audits where same inventory would go to every specialist.

For material prompt compression, `challenger` must independently inspect obligation map and cost/quality decision. Specialist disagreement is evidence to reconcile, not vote.

### 06: Run shared quality gates

Follow `../../shared/helper-cli-contract.md` and `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/run_gates.py --help`. Use project-configured lint, format, type, test commands for discovered surfaces, explicit reasons for inapplicable gates, clean diff review.

### 07: Detect drift and broken references

Run `rg -n` for `config_file|skills/|roles/|quality-gates|run_gates.py|write-result.py` over existing `AGENTS.md`, `.codex`, `.agents`, and optional target. Write results to `<run-directory>/reference-scan.txt`; record missing inputs or command failure explicitly.

**Structural context (optional)**: when audited scope contains Python package, also probe codemap-py once for undocumented public surface and externally-uncalled modules: `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/codemap_adapter.py context --category audit --out <run-directory>/codemap-context.json`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with reference scan above, using persisted evidence as additional signal, never replacement for it.

### 08: Audit spawn-pattern coverage and overlap in `AGENTS.md` (instruction-level check)

Run two separate `rg -n` argv scans: `delegat|specialist|spawn|role|\[agents\.` over existing `AGENTS.md` and `.codex/config.toml`, writing `<run-directory>/spawn-sections.txt`; then `Trigger and skip boundaries|TRIGGER when|SKIP when|NOT for` over supplied target or `AGENTS.md`, writing `<run-directory>/spawn-policy-sections.txt`. Record missing inputs or command failure explicitly.

### 09: Review native skill and agent contract consistency

Audit each plugin against its own declared skill and role contracts, read from its shipped instructions and authoritative local configuration. Do not impose Codex Rig's section names, artifact lifecycle or calibration roster on another plugin merely because it is audited here. Missing an applicable declared contract is substantive; a different supported heading or workflow format is not drift. Record the owning contract and any genuine unmet obligation in the audit ledger.

Codex Rig artifact skills declare:

- `Input Schema`
- `Workflow`
- `Fail-Fast Rules`
- `Quality Gates`
- `Calibration Hooks`
- `Output Contract`

Codex Rig configured roles or agents declare:

- `## Scope` or clear role boundary text
- `## Evidence Standard`
- `## Boundaries`
- `## Output Contract` or explicit output format

### 10: Review role-roster consistency when a role-card target is supplied

When target is supplied, run `rg -n` for `^(role_id|name|model|description|developer_instructions)` over that exact target and write `<run-directory>/role-roster-scan.txt`; record scan failure. Without target, create that artifact as empty file and classify role roster as not configured.

Classify overlap as `keep`, `sharpen`, `merge-prune`:

- `keep`: distinct decision surface.
- `sharpen`: role stays; tighten boundary.
- `merge-prune`: no distinct acceptance criterion.

### 11: Classify findings using `../../shared/severity-map.md`

### 12: Write mandatory result artifact

Use shared lifecycle/authoritative help. Write `AUDIT_METADATA`, validate `audit`, promote only validated candidate.

## Fail-fast Rules

01. Requested target missing or escaping consuming project/approved external scope => fail.
02. Shared gate script missing => fail.
03. Critical-path broken config/skill reference => fail.
04. Any configured role/agent lacks routing coverage => fail.
05. Unclear/overlapping spawn intent lacks collaboration-team guidance => fail.
06. Agent overlap lacks keep/sharpen/merge-prune decision => fail.
07. Configured entry lacks its declared skill/role contract section => fail unless exception recorded.
08. Non-native runtime assumptions in audited skill or role card => fail.
09. Result artifact missing => fail.
10. `prompt-efficiency.md` or its required evidence sections missing => fail.
11. Prompt candidate accepted from raw length/proxy alone, without obligation mapping, hard value guards, loaded-reference cost, required adversarial review, matched live identity, or declared material reduction => fail.
12. `axis=value-per-token` requested without `scope=skills|all` or supplied target containing skill root => fail.

## Quality Gates

Required checks:

- `review`: inventory, contract ledger, workflow-exploration map and transitions, focused counterexamples, evidence-triggered context rechecks and final coverage reconciliation (or explicit inapplicability), prompt-efficiency evidence, reference scan, overlap decisions, `git diff --check`.
- `calibration`: run owning project's declared calibration command when audited workflow behavior changes; for Codex Rig source, use `runtime/calibration/run.py --layout plugin`.

Conditional checks:

- `lint`/`format`: when Python/TOML/shell/Markdown formatters available.
- `tests`: with executable probes/behavior-changing fixes.

## Calibration Hooks

Update calibration when audit scope, contract requirements, or routing checks change:

- benchmark patterns: `audit`, every configured skill, every configured agent
- behavioral cases: unjustified stops, equivalent-input continuation, automatic complementary recovery with substantive negative controls, mapped-flow coverage, invalidated assumptions revisited in affected consumers, bounded static-target inapplicability, runtime leak detection, stale reference handling, overlap classification, unsafe sync recommendation, length-only compression, missing loaded-reference cost, missing paired evidence, critical behavior regression, and below-threshold cost reduction

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared frame with `Next steps`. `Outcome`: `accepted|rejected|insufficient-evidence`. `Results`: exactly `Item | Severity / impact | Decision | Evidence | Next action`, one row/material item. `Remaining`: owner and closure action.

`AUDIT_METADATA.value_per_token` records schema version 2, status (`not-run|insufficient-evidence|rejected|accepted`), scope roots, baseline/candidate hashes, static measurements, conditional-load trace, obligation-map path, static gates, behavioral/live comparisons, decision, and residual limits.

Minimum artifact payload template: `result-template.json`.

---
name: audit
description: Audit Codex configuration, workflow, and prompt-efficiency (instruction cost, value-per-token) drift; emit ranked gaps and measurable gates.
---

# Audit

Run linear configuration/workflow audit.

## Input Schema

```json
{
  "scope": "config|skills|roles|all",
  "target": "optional path",
  "mode": "upgrade|adversarial",
  "axis": "standard|value-per-token",
  "min_cost_reduction": "optional fraction; default 0.05 for value-per-token acceptance",
  "skip_gate": false,
  "done_when": "drift and broken references are ranked with gate result; fix level chosen interactively unless skip_gate=true"
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

Run `rg --files` with the `AGENTS.md`, `.codex/config.toml`, and `.agents/skills/**` globs as argv. When `target` is supplied and exists, enumerate regular files under it to depth four with platform-native filesystem walk. Sort and deduplicate both result sets into `<run-directory>/inventory.txt`; record unavailable inputs or collection failures.

### 03: Build an audit ledger before running gates

Write `<run-directory>/audit-ledger.md` with these sections:

- `Inventory`: configured/present policy, skills, and role-routing surfaces.
- `Broken References`: missing files, stale paths, unresolved shared resources.
- `Runtime Leaks`: non-native runner fields/external runtime assumptions.
- `Coverage`: calibration benchmark/behavior.
- `Overlap`: duplicate/fuzzy ownership decisions.
- `Prompt Efficiency`: instruction cost, loaded context, obligation preservation, and value-guard evidence.
- `Recommendations`: ranked fixes.

### 04: Audit prompt efficiency without using length as quality

Always write `<run-directory>/prompt-efficiency.md` with `Measurement`, `Cost Baseline`, `Loaded Context`, `Obligation Map`, `Value Guards`, `Adversarial Review`, and `Recommendations` sections. For `scope=config|roles` with no skill target, record `not-applicable` and why. For `scope=skills|all` or `axis=value-per-token`, audit each discovered local skill root independently; absent optional Codex Rig, Codemap, or Bridge root is `not-configured`, never cross-plugin dependency or failure.

Measure with matched provider-native token counts; else local `tiktoken` with `o200k_base`; else deterministic UTF-8 bytes and words. Do not install tokenizer or use network access. Counts are cost evidence, never quality evidence; proxy-only comparison cannot accept candidate and remains `insufficient-evidence`.

For each baseline/candidate pair, use same file set and record hashes, measurement source, static cost, loaded referenced instructions, conditional-load decisions, and cost of every reference required by exercised path. A moved instruction is not saving when same run must load it. Map every baseline obligation to its candidate location and evidence, covering safety, ordering, user approval, output/schema, fail-fast, tool-permission, and quality-gate requirements.

Value Guards must record exact package/tests, behavioral and calibration results, contract-marker coverage, tool/check failures, and completion quality. A live comparison must use paired tasks with same model, effort, task contract, and prompt identity; record native token/cost fields and confidence limits. Treat missing paired live evidence as `insufficient-evidence` for material behavior claim.

Any candidate that removes, moves, or condenses obligations requires adversarial review of obligation map. Accept only when all hard guards pass, no critical behavior regresses, tool/check failures do not increase, and normalized cost falls by `min_cost_reduction`; otherwise reject or mark insufficient evidence. A shorter candidate fails when it loses obligation, weakens guard, hides loaded-reference cost, or lacks required evidence. Value-per-token scores may rank already accepted candidates but never override hard gates.

### 05: Route specialists only when triggered

For `scope=all`, `mode=adversarial`, audits crossing skills, agents, CI/config, or material prompt compression, read and apply `../../shared/specialist-orchestration.md`; do not load it for narrow single-surface audit. Write `<run-directory>/specialist-audit-plan.md` packs for:

- `curator`: skill/agent/config drift, duplication, calibration hygiene.
- `linting-expert`: Markdown, Python, shell, ruff/mypy/pre-commit references.
- `cicd-steward`: CI harness, workflow permissions, artifact behavior.
- `challenger`: adversarial check of no-finding or low-risk conclusions.

Stay single-agent for narrow `scope=config`, `scope=skills`, or `scope=roles` audits where same inventory would go to every specialist.

For material prompt compression, `challenger` must independently inspect obligation map and cost/quality decision. Specialist disagreement is evidence to reconcile, not vote.

### 06: Run shared quality gates

Follow `../../shared/helper-cli-contract.md` and `python PLUGIN_ROOT/shared/run_gates.py --help`. Use project-configured lint, format, type, and test commands for discovered surfaces, explicit reasons for inapplicable gates, and clean diff review.

### 07: Detect drift and broken references

Run `rg -n` for `config_file|skills/|roles/|quality-gates|run_gates.py|write-result.py` over existing `AGENTS.md`, `.codex`, `.agents`, and optional target. Write results to `<run-directory>/reference-scan.txt`; record missing inputs or command failure explicitly.

**Structural context (optional)**: when audited scope contains Python package, also probe codemap-py once for undocumented public surface and externally-uncalled modules: `python PLUGIN_ROOT/shared/codemap_adapter.py context --category audit --out <run-directory>/codemap-context.json`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with reference scan above, using persisted evidence as additional signal, never replacement for it.

### 08: Audit spawn-pattern coverage and overlap in `AGENTS.md` (instruction-level check)

Run two separate `rg -n` argv scans: `delegat|specialist|spawn|role|\[agents\.` over existing `AGENTS.md` and `.codex/config.toml`, writing `<run-directory>/spawn-sections.txt`; then `Trigger and skip boundaries|TRIGGER when|SKIP when|NOT for` over supplied target or `AGENTS.md`, writing `<run-directory>/spawn-policy-sections.txt`. Record missing inputs or command failure explicitly.

### 09: Review native skill and agent contract consistency

Each configured skill has:

- `Input Schema`
- `Workflow`
- `Fail-Fast Rules`
- `Quality Gates`
- `Calibration Hooks`
- `Output Contract`

Each configured role or agent has:

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

- `review`: inventory, contract ledger, prompt-efficiency evidence, reference scan, overlap decisions, `git diff --check`.
- `calibration`: run owning project's declared calibration command when audited workflow behavior changes; for Codex Rig source, use `runtime/calibration/run.py --layout plugin`.

Conditional checks:

- `lint`/`format`: when Python/TOML/shell/Markdown formatters available.
- `tests`: with executable probes/behavior-changing fixes.

## Calibration Hooks

Update calibration when audit scope, contract requirements, or routing checks change:

- benchmark patterns: `audit`, every configured skill, every configured agent
- behavioral cases: runtime leak detection, stale reference handling, overlap classification, unsafe sync recommendation, length-only compression, missing loaded-reference cost, missing paired evidence, critical behavior regression, and below-threshold cost reduction

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared frame with `Next steps`. `Outcome`: `accepted|rejected|insufficient-evidence`. `Results`: exactly `Item | Severity / impact | Decision | Evidence | Next action`, one row/material item. `Remaining`: owner and closure action.

`AUDIT_METADATA.value_per_token` records schema version, status (`not-run|insufficient-evidence|rejected|accepted`), scope roots, baseline/candidate hashes, static measurements, conditional-load trace, obligation-map path, static gates, behavioral/live comparisons, decision, and residual limits.

Minimum artifact payload template: `result-template.json`.

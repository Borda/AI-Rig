---
name: manage
description: 'Manage Codex agents, skills, or config entries: create, update, or remove with guardrails.'
---

# Manage

Guarded Codex config management for agents, skills, rules, and local config.

The installed plugin tree is immutable input. Resolve requested targets against the consuming project or an explicit user-approved external scope; never edit this skill's plugin cache, packaged role cards, shared helpers, runtime assets, or package manifests. Codex Rig agent links are managed only by the bundled `agent-shims` workflow, not by this general-purpose skill.

## Input Schema

```json
{
  "intent": "create|update|delete|rename|add-permission|remove-permission",
  "target": "required agent, skill, rule, config key, or path",
  "change": "required description or spec path",
  "execution": "optional auto|serial|parallel-read|parallel-write; default auto",
  "done_when": "target and all references are updated or explicitly left unchanged"
}
```

## Parallel Adoption (Portable read-only)

This skill permits only its promoted portable read-only route. Resolve execution precedence from per-invocation `--execution=<mode>`, then `CODEX_RIG_EXECUTION`, then the `auto` default. The default execution mode is `auto`. `auto` selects this route only after this consumer's runtime matrix and promotion; otherwise it resolves safely to `serial`. Every write still requires a frozen plan and exact-digest approval. This route never bypasses consumer promotion, serial parent authority, or write approval.

Follow the [canonical G0–G8 execution flow](../../ARCHITECTURE.md#canonical-g0g8-execution-flow) for the shared gate order and fork outcomes. This consumer's read-only inventory passes are bounded by G0–G5; the parent owns deterministic G6 integration, G7 verification, and G8 verdict/promotion.

### Safe parallel work

The promoted route permits read-only inventory, reference, ownership, and policy-impact scans over immutable disjoint targets with separate outputs. Disjoint propagation is not enabled; config, policy, documentation, calibration, cache, generated-output, artifact, and result writes remain outside this portable route.

### Required barrier

Apply the shared [host compatibility check](../../shared/specialist-orchestration.md#host-compatibility-before-dispatch) before preparing any child work. Include `read_host` only from verified launcher-supported child controls. Missing or incompatible controls make `auto` resolve serial with a reason; explicit parallel-read stops before dispatch. A plan declaration is not effective-control proof, and post-run validation remains mandatory.

Before any dispatch, freeze the intent, target, baseline, ownership map, exact references, calibration and routing impact, context packs, role-card hashes, checks, resource locks, and plan digest. Dispatch at most one fixed dependency-ready wave, then join every terminal scan before edits, propagation, gates, or acceptance; changed scope requires a new plan.

The frozen `<run-directory>/execution-plan.json` must include exact `consumer_policy` values `consumer_id=manage`, `capability=portable-read-only`, `promotion_status=promoted`, `parent_mutations=serial`, and `canonical_gates=serial`. It must also include `write_policy`: use `parent_writes=planned` with `approval_requirement=exact-plan-digest` when any parent mutation is planned, otherwise `parent_writes=none` with `approval_requirement=not-required`. A planned write requires `<run-directory>/write-approval.json` containing only the exact plan SHA-256, `response=approve`, and `source=explicit-input|user-prompt`.

Before dispatch, run `python PLUGIN_ROOT/shared/parallel_execution.py preflight --consumer manage --plan <run-directory>/execution-plan.json --approval <run-directory>/write-approval.json`; append `--execution=<mode>` only for an explicit invocation value. Omit `--approval` only when the frozen write policy declares no parent writes. A nonzero result stops the route.

After every spawned child reaches a terminal handoff, run `python PLUGIN_ROOT/shared/parallel_execution.py validate-runtime --consumer manage --manifest <run-directory>/execution-manifest.json --plan <run-directory>/execution-plan.json --parent-rollout <authoritative-parent-rollout> --sessions-dir <authoritative-sessions-directory> --run-dir <run-directory> --roles-dir PLUGIN_ROOT/roles`. Require `runtime_promotion_eligible=true`, `consumer_id=manage`, and `write_parallel_eligible=false`. Run the same preflight again after the terminal join and before the first parent mutation. Any plan, approval, consumer, runtime, or join drift stops mutation and requires a new frozen plan plus exact approval.

### Serial parent decisions

The parent owns all create, update, delete, rename, and permission mutations; same-file policy or config changes; shared `AGENTS.md`, README, and config edits; calibration version advancement; propagation; artifacts and result writes; canonical quality gates; verdict; and promotion. Installed plugin-root and generated `codex-rig-*.toml` safety decisions remain serial and unchanged.

### Resource conflicts

Declare only validated resource locks such as `git-index`, `cache:<path>`, `generated:<path>`, and `test-env:<name>`. Shared targets, paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.

### Fallback

Unavailable or unsafe fan-out uses equal-gate `serial-fallback` from the same frozen plan with the same quality gates and retained evidence. Never label fallback as parallel or weaken checks because dispatch was unavailable.

### Acceptance

This skill's shared runtime matrix and consumer promotion must remain complete before `auto` selects this route. Acceptance must prove freeze, complete join, truthful execution label, resource compatibility, equal gates, and unchanged serial parent authority.

### Stop rule

Generic parallel writes remain disabled. Stop without dispatch on missing promotion, mutable packs, ownership or resource overlap, sensitive or unproven controls, missing terminal evidence, or an incomplete join; create, update, delete, rename, permission, and all other mutations remain parent-serial.

## Workflow

### 01: Create run directory

Run `create_run.py --skill manage` per `../../shared/helper-cli-contract.md`.

### 02: Parse intent and target

Intents:

- `create`: scaffold a new skill/agent/rule/config entry.
- `update`: edit an existing target.
- `rename`: move target and update references.
- `delete`: remove target only after dependency scan.
- `add-permission` / `remove-permission`: modify permission policy with rationale.

Unknown intent => fail before edit.

### 03: Resolve owned files and blast radius

Run `rg --files` with the `AGENTS.md`, `.codex/**`, and `.agents/**` globs as an argv command; sort its lines and write them to `<run-directory>/inventory.txt`. Run `rg -n` for the exact target over `.codex`, `.agents`, and `AGENTS.md`, write results to `<run-directory>/references.txt`, and record unavailable inputs or command failure explicitly.

Write `<run-directory>/ownership.md`: exact edited and intentionally untouched files.

Resolve the current skill path and reject any target whose canonical path is inside the same installed plugin root. Reject generated `codex-rig-*.toml` targets here even when they are outside the cache; report the dedicated lifecycle workflow as the only permitted owner.

### 04: Run safety gates before editing

Deletion safety required for `delete` and `rename`.

- Delete/rename: no unresolved references or explicit migration plan.
- Permission changes: reason, use case, risk note.
- Public behavior change: consider docs/routing/calibration.
- Versioned calibration fixtures are committed-history markers: compare current value to `git show HEAD:<path>`; while uncommitted, advance at most one step from last committed value.
- A new-commit request never authorizes rewriting an existing commit. Amend, rebase, reset, squash, fixup, and equivalent history edits require an explicit request for that exact operation.
- Home sync out of scope unless explicitly requested.

### 05: Apply the smallest reversible edit

Use Codex's native scope: `.agents/skills/` for repository skills, `AGENTS.md` for repository guidance, and `.codex/config.toml` for project runtime settings. Use custom agent-config paths only when the active Codex contract and user request require them. Never infer that the source repository is present from the installed cache layout.

### 06: Propagate references

For behavior changes, update relevant descriptions, mappings, routing text, calibration notes in same patch. List intentionally stale references in `<run-directory>/unresolved-references.md`.

For broad changes with separable config, docs, calibration, or verification workstreams, route through `delegation-lead` and shared specialist orchestration. Use the lowest-cost capable canonical role per bounded workstream. Accept delegated change only after the handover gate proves ownership, objective evidence, applicable checks, visible unresolved limits, and parent-owned final acceptance.

### 07: Run shared quality gates

Inspect `python PLUGIN_ROOT/shared/run_gates.py --help`. Supply real affected-surface commands and explicit reasons for every inapplicable gate; never use `true` as skip reason. Review includes clean diff check.

### 08: Write mandatory result artifact

Manage artifacts include `ownership.md`; follow `../../shared/helper-cli-contract.md`. Write with `MANAGE_METADATA`, validate as `manage`, promote only validated candidate.

## Fail-Fast Rules

1. Missing intent or target => fail.
2. Delete/rename with unresolved references => fail.
3. Permission/config change without rationale => fail.
4. Behavior change without routing/docs/calibration decision => fail.
5. Result artifact missing => fail.
6. Target resolves inside the installed plugin root => fail without editing.
7. Target is a generated `codex-rig-*.toml` role link => fail and route to the dedicated lifecycle workflow.
8. Existing history would be rewritten without an explicit request for that exact operation => fail.

## Quality Gates

Required:

- `review`: inventory diff, reference scan, `git diff --check`.
- `artifact`: `ownership.md`, gate logs, result JSON pass shared validator.

Conditional:

- `lint`/`format`: when generated Python, TOML, shell, or Markdown formatters available.
- `tests`: calibration or smoke checks for changed skills/agents when behavior changes.

## Calibration Hooks

Behavior-changing management edits update or explicitly review the owning project's configuration, tests, documentation, routing, and calibration fixtures. Codex Rig maintainers use `PLUGIN_ROOT/runtime/calibration/` and `PLUGIN_ROOT/shared/native-skill-contract.md`; an installed plugin cache remains immutable.

Calibration must reject premature portable read-only runtime opt-in, generic write authorization, mutable or unjoined scan packs, delegated management mutations, and quality-gate parallelism without executable resource-isolation evidence.

For versioned calibration artifact changes, calculate version from last commit, not dirty worktree. If `HEAD` has `1.3`, all next-commit uncommitted edits stay `1.3` or `1.4`: one version step only; do not bump to `1.5`, `1.6`, etc. before a commit.

Commit-output management also keeps the owning project's commit-response contract aligned with required message shape. In AI-Rig, the canonical packaged contract is `../../shared/commit-response-template.md`:

```text
<type>(<scope>): <title>

Changes:
- <complete meaningful change description>

Impact:
- <concrete effect>

Verification:
- <exact check and result>

Residual limits:
- <remaining limit or "None known">

---

Co-authored-by: Codex <codex@openai.com>
```

Keep `Verification:` change-specific and compact: include only final checks that materially validate the committed surfaces, consolidate related gates, and omit exploratory probes, failure-first reproductions, repeated reruns, setup diagnostics, and unrelated broad checks. The canonical contract is authoritative for the full exclusion and not-run rules.

## Output Contract

Before writing the result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows the shared ordered frame. `Outcome` is a completed, rejected, or blocked management action. `Results` has one changed or evaluated surface per row and exactly `Surface | Outcome | Verification | Remaining limit`. Apply the shared `Verification`, `Remaining`, `Next steps`, `Confidence`, and supplemental `Artifact` rules; include ownership/policy checks and every required human action with owner.

Minimum artifact payload template: `result-template.json`.

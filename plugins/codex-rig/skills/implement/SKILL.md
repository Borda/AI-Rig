---
name: implement
description: Implement changes with a linear plan-build-verify workflow and measurable quality gates.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Implement

See the [fixed recurrence and root-cause policy](../../shared/native-skill-contract.md#recurrence-and-root-cause-policy) and [reasoning-progress escalation policy](../../shared/native-skill-contract.md#reasoning-progress-escalation) for repeated-obstacle handling; record and validate `reasoning-progress.json` before another cycle after escalation trigger.

Run linear implementation with strict gates.

Keep setup and review artifacts subordinate to the user's acceptance condition under the primary-goal rules in Reasoning-Progress Escalation. Once the fix is established, write the minimal implementation and regression before another reviewer-setup cycle. One failed bounded setup repair stops that auxiliary route; continue safe authorized implementation and retain the coverage limit without claiming clean completion.

When independent review findings are fixed in a cycle, read `../../shared/adversarial-loop.md` and apply its bounded convergence and stop rules. Use the `challenge-resolve` skill for an explicitly requested standalone loop; this workflow still owns its normal completion gates.

## Input Schema

```json
{
  "goal": "required implementation objective",
  "mode": "feature|fix|refactor|config|spike",
  "constraints": [
    "optional constraints"
  ],
  "execution": "optional auto|serial|parallel-read|parallel-write; default auto",
  "done_when": "required acceptance statement"
}
```

## Lightweight Local Work

> Read and apply [Proportional Execution](../../shared/native-skill-contract.md#proportional-execution) before choosing the direct or detailed path.

Use a parent-only path for a small, understood, reversible local change with bounded impact and existing task authorization. A code fix plus its regression test remains one change. Routing examples are optional; tests and documentation for one change do not create separate domains.

1. Inspect the affected flow and applicable instructions; define the requested outcome. Investigate an unknown cause before editing.
2. Make the smallest authorized change. Run the relevant acceptance check and configured lint/format checks; inspect the diff.
3. Report the outcome, verification, and material limits concisely. Do not create execution plans, specialist packs, confidence worksheets, five-entry gate bundles, or final-handoff/result artifacts solely for this path. Reuse still-valid checks for unchanged source and environment.

This path takes precedence over the detailed workflow and role examples below. Select the detailed workflow for medium/large or unexpectedly nontrivial work under Proportional Execution, an explicitly requested structured report/review, or work requiring independent specialist coverage. Actual runtime permissions, protected-state decisions, and project-required checks still apply. Missing portable parallel controls select parent-serial work for that route; they do not prohibit an authorized native bounded delegation route or require another permission request for already-authorized local edits.

## Execution Route Selection

Before preparing or dispatching any child, resolve invocation `--execution=<mode>` → `CODEX_RIG_EXECUTION` → `auto` and apply the loaded Proportional Execution selector. Honor `serial`; `parallel-read` chooses only the strict portable section; reject unsupported `parallel-write`. Absent, explicit, or environment `auto` chooses suitable observed native bounded work or parent-serial work, never the portable route. Native writes require the accepted plan and disjoint task-authorized ownership.

Record the choice and reason in the existing plan or concise outcome. Use portable helpers and digest receipts only after selecting `parallel-read`; native work does not inherit their helper-level `auto` resolver.

## Native Bounded Delegation

After the accepted plan stages in Proportional Execution, native collaboration may assign task-authorized implementation, test, or documentation edits to disjoint owners. Apply `../../shared/specialist-orchestration.md`; give each owner its file set, behavior, acceptance checks, dependencies, and stop condition. Reuse the accepted parent plan rather than requiring recursive child planning. Inspect actual deltas and terminal handoffs, then integrate and verify serially. Shared files, coupled dependencies, protected decisions, canonical gates, and final acceptance stay parent-owned. Instruction bounds do not establish filesystem isolation or portable promotion.

This route uses existing task authorization and observed tool capabilities, separately from the explicit portable modes below. Never relabel native work as portable parallel-read or generic `parallel-write`, or use it to bypass selected portable gates.

## Parallel Adoption (Portable read-only)

<!-- policy-sibling: skills/manage/SKILL.md (Parallel Adoption section) — near-duplicate, invariant text in shared/portable-parallel-read.md; also see skills/code-review/SKILL.md Workflow parallel-review section as a third divergent variant; check siblings before editing this section alone -->

> Only when Execution Route Selection resolves to `parallel-read`, read [portable-parallel-read.md](../../shared/portable-parallel-read.md) before any child preparation; `serial`, `auto`, and native routes do not load it. The reference preserves the shared route gate, write-policy and approval rules, host-compatibility barrier, preflight and runtime-validation qualifiers, resource-lock declaration, fallback, acceptance, and stop rule; the consumer values below stay authoritative for this skill.

This portable section permits only its promoted portable read-only route; its restrictions do not govern the separate Native Bounded Delegation route.

Follow the [canonical G0–G8 execution flow](../../ARCHITECTURE.md#canonical-g0g8-execution-flow) for shared gate order and fork outcomes. This consumer's read-only evidence passes are bounded by G0–G5; parent owns deterministic G6 integration, G7 verification, and G8 verdict/promotion.

### Safe parallel work

The promoted route permits read-only evidence, acceptance, and documentation-impact passes with immutable disjoint context packs and separate outputs. Source, test, documentation, configuration, calibration, cache, generated-output, artifact, and result writes are outside this portable route.

### Required barrier

Before any dispatch, freeze goal, mode, `done_when`, baseline, ownership DAG, context packs, role-card hashes, checks, resource locks, and plan digest. Dispatch at most one fixed dependency-ready wave, then join every terminal handoff before implementation, integration, gates, or acceptance; changed scope requires new plan.

The frozen `<run-directory>/execution-plan.json` must include exact `consumer_policy` values `consumer_id=implement`, `capability=portable-read-only`, `promotion_status=promoted`, `parent_mutations=serial`, and `canonical_gates=serial`.

Bootstrap exception: run-directory creation (Step 01), baseline diff/branch persistence (Step 02), and the `write-approval.json` write itself are exempt from requiring a prior approved plan digest — these precursor writes must exist before a plan digest can be computed or approved.

Before dispatch, run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/parallel_execution.py preflight --consumer implement --plan <run-directory>/execution-plan.json --approval <run-directory>/write-approval.json`; pass `--execution=parallel-read` for the selected portable route, including an environment-selected value.

After every spawned child reaches a terminal handoff, run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/parallel_execution.py validate-runtime --consumer implement --manifest <run-directory>/execution-manifest.json --plan <run-directory>/execution-plan.json --parent-rollout <authoritative-parent-rollout> --sessions-dir <authoritative-sessions-directory> --run-dir <run-directory> --roles-dir PLUGIN_ROOT/roles`. Require `runtime_promotion_eligible=true`, `consumer_id=implement`, and `write_parallel_eligible=false`.

### Serial parent decisions

On this portable route, parent owns all implementation, test, and documentation writes; shared files and dependency chains; integration and conflict handling; calibration, artifacts, and result writes; canonical quality gates; verdict; and promotion. Never expand wave dynamically or dispatch dependent work early.

### Resource conflicts

Shared paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.

### Stop rule

Generic portable parallel writes remain disabled.

## Workflow (Exact Commands)

### 01: Create run directory

Run `create_run.py --skill implement` per `../../shared/helper-cli-contract.md`. Apply Execution Route Selection before any child preparation. Native or parent-serial work follows its selected route; only selected `parallel-read` work enters Parallel Adoption and its helper/receipt lifecycle.

### 02: Record baseline diff and branch

Run `git rev-parse --abbrev-ref HEAD` as argv command and write stdout to `<run-directory>/branch.txt`.

Inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/collect_diff.py --help`; collect `working-tree` into `<run-directory>/baseline`.

### 03: Route the change type and define ownership

Modes:

- `feature`: define public behavior, acceptance checks, docs impact, and tests before implementation.
- `fix`: reproduce or cite failing behavior before editing.
- `refactor`: preserve behavior with characterization tests or equivalent safety net.
- `config`: inventory references and calibration/routing impact before editing.
- `spike`: read-only or disposable probe; do not present as completed implementation.

Define narrowest reversible change, owners, acceptance. Assign native bounded owners here, after accepted plan details and before Step 05. Apply Proportional Execution: finish both independent plan challenges before Step 05 for medium/large or promoted work; reuse accepted parent-scoped plans for bounded child tasks.

**Structural context (required for Python targets)**: select one task-neutral route at decision point, then invoke adapter once: `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/codemap_adapter.py context --category implementation --query-kind <kind> [--target <qname>] --out <run-directory>/codemap-context.json`. Invoking it is mandatory whenever target or planned files include a `.py` path; a `skip` route still runs the adapter and persists its `skipped` artifact, never no artifact. Use `skip` for exact localized edit with no unresolved structural fact, matching single route (`central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling`) for one unresolved fact, and `standard` for broad or unknown scope. Map direct, all, or production caller questions to `callers`; use `blast` only for explicitly transitive caller questions. An explicit user or tool request for structural evidence overrides `skip`. Per `../../shared/codemap-contract.md`, provider absence/incompatibility stays non-fatal — the artifact records status and reason, then continue with routing above. Persist result once here, before step 05 implementation; step 06 specialist fan-out consumes `<run-directory>/codemap-context.json`, plus at most 3 targeted follow-ups per specialist under the contract's `Specialist follow-up queries` rule, each cited in that specialist's own evidence.

### 04: Run the anti-rationalization gate before editing

- Existing code and tests for target surface have been read.
- Failure mode or new behavior is captured by failing doctest, pytest, or explicit acceptance check.
- Coding changes have project coding-principles plan from applicable `AGENTS.md` layers: simple/readable/reproducible structure first, short reusable units without low-value argument-remapping wrappers, guard clauses or early `return`/`yield`/`continue` for invalid or terminal cases, project docstring-style detection, concise purpose docstrings, and inline comments only for non-trivial implementation blocks.
- `feature` mode has feature demo contract before production edits:
  - simple public API: inline doctest or focused pytest that shows intended call and result
  - multi-step behavior: minimal example or pytest exercising user-visible workflow end to end
  - demo must be automatically executable and must fail against current code for intended missing behavior
  - if demo passes before implementation, stop and re-scope; do not silently proceed unless user explicitly overrides gate
- Review demo contract for goal alignment, API shape, missing scenarios, and automatic verifiability before implementation.
- If task starts from symptom, failing test, failing CI, flaky behavior, regression, tool/environment error, or unexplained metric shift, run `investigate` first or document equivalent root-cause evidence before editing.
- Root-cause evidence includes claim, supporting logs/code, falsification check, and at least one rejected alternative. A workaround-only change is temporary mitigation, not completion, unless explicitly requested by user.
- Behavior-preserving refactors have characterization tests or equivalent current-behavior safety net.
- The next edit is smallest reversible step, not speculative refactor.

### 05: Implement minimal change

This step's "implement" produces the draft/proposal work product for the changed surface; it is not a final merge-ready change until specialists join and the terminal review gate at Steps 08-09 completes.

While implementing, keep code understandable from code itself:

- Apply consolidated project coding principles from applicable `AGENTS.md` layers.
- Refactor long, dense, or deeply nested blocks into named helpers/classes before adding explanatory text.
- Avoid tiny rarely used helpers that only remap arguments; keep logic inline, use local helper, or use `functools.partial` when only binding arguments.
- Match project's configured or established docstring style, and keep function/class purpose in docstrings rather than comments directly above definitions.
- Refactor instead of writing long docstrings or comments when block needs long explanation to be understandable.

Failing-first and acceptance pytest runs in this loop follow [Sandboxed Test Runs](../../shared/native-skill-contract.md#sandboxed-test-runs): add `-p no:xdist` when its conditions hold, and request the reusable pinned test-runner approval only when its trigger holds, once, before the first escalated run.

### 06: Orchestrate specialists when the change crosses a domain boundary

Read and apply `../../shared/specialist-orchestration.md` only when task crosses domains, benefits from independent verification, or splits into parallel context packs; do not load it solely for a narrow, understood one-domain change. File count alone does not determine routing. Native bounded owners are assigned in Step 03; this step retains any required subsequent specialist review.

Before spawning or substituting specialists, write `<run-directory>/specialist-plan.md` with one row per planned pass:

| role | trigger | context pack | expected output | mode |
| -- | -- | -- | -- | -- |

Optional routing examples for work that actually benefits from separate expertise:

- public API or architecture: `sw-engineer` returns a proposal for review, `qa-specialist` for acceptance matrix, and `doc-scribe` for public docs/docstrings when applicable. Use `solution-architect` only when the user expressly requests that advisory pass or selects the role; it returns a bounded read-only design artifact to the Sol parent/session, which continues and accepts.
- bug fix or regression: `investigate` or equivalent root-cause evidence first, then `sw-engineer` for fix and `qa-specialist` for failure-before/pass-after proof.
- CI/tooling: `cicd-steward` for workflow behavior and `linting-expert` for ruff/mypy/pre-commit or suppression policy.
- security-sensitive code: the Sol parent/session scopes risk before implementation and pairs `sw-engineer` with `qa-specialist` as needed. Use read-only `security-auditor` only when the user expressly requests that advisory pass or selects the role; it returns bounded evidence to the Sol parent/session, which continues and accepts.
- ML/data/research behavior: `data-steward` for data contracts, `scientist` for method/metric validity, `squeezer` for performance claims, plus `qa-specialist` for tensor boundary tests.
- docs-impacting behavior: `doc-scribe` gets only verified public behavior, API signatures, examples, and migration notes; do not send unrelated implementation details.
- high-risk or broad changes: `challenger` runs after draft plan or diff to stress-test assumptions and residual risk.

Each specialist context pack must include only relevant files, hunks, logs, and questions. Do not give every specialist full task history. If specialist fan-out is unavailable, record in-main substitute in `<run-directory>/specialist-notes.md` and lower confidence when independence mattered.

### 07: Write `<run-directory>/development-notes.md` before running gates

Required sections:

- `Scope`
- `Acceptance Criteria`
- `Evidence`
- `Specialist Policy`
- `Gates`

### 08: Run shared quality gates

Inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/run_gates.py --help`, then run all project-relevant gates with explicit commands or skip reasons.

### 09: Review the changed files and the gate output before deciding pass/fail

### 10: Classify findings using `../../shared/severity-map.md`

### 11: Record confidence evidence before the detailed final handoff

Write `<run-directory>/confidence-calibration.md` with these sections:

- `Initial Confidence`: starting score and concrete uncertainty sources.
- `Objective Evidence`: code paths read, tests/checks run, reproduction or acceptance evidence, and artifacts inspected.
- `Confidence Gaps`: missing evidence, unverified assumptions, risky substitutions, or unavailable checks.
- `Recovery Actions`: internal loops already performed to increase confidence, such as reading more source, running focused checks, adding/adjusting tests, consulting specialist policy, or reducing scope.
- `Recomputed Confidence`: final score after recovery, with why it is objectively supported.
- `Remaining Limits`: residual uncertainty and why it is acceptable or blocking.

Shared confidence policy:

Apply shared confidence band policy from `../../shared/quality-gates.md`. This skill records required evidence in `confidence-calibration.md` and mirrors it in `IMPLEMENT_METADATA.confidence_recovery` before output.

Confidence must be honest and objectively verifiable. Do not inflate it to pass gate; if evidence is missing, keep lower score and fail or time out with missing evidence named.

### 12: Write and validate the mandatory result artifact

Completion commit: run the [Local Git approval grant](../../shared/native-skill-contract.md#local-git-approval-grant) check. `grant …` → commit the verified, owned changes with the shared commit template unless the user asked for no commit, leave unstaged or to wait; `no grant` → an explicit commit request commits after checks, otherwise ask that section's one question (Approve / Approve always / Deny) before committing, and **Deny** leaves the changes unstaged. Record the check result, any answer and the commit disposition in the handoff.

Follow `../../shared/helper-cli-contract.md` and authoritative help. Write with `IMPLEMENT_METADATA`, validate as skill `implement`, and promote only validated candidate.

`IMPLEMENT_METADATA.confidence_recovery` must mirror `confidence-calibration.md` and include `initial_confidence`, `final_confidence`, `status`, `evidence`, `recovery_actions`, and `remaining_limits`. `IMPLEMENT_METADATA.confidence_gap_closures` must include one closure record per non-empty `confidence_gaps` entry, with `status=closed|unresolved|deferred` and matching evidence or rationale.

## Fail-fast Rules

01. Missing `goal` or `done_when` => fail.
02. Shared gate script missing => fail.
03. Any critical finding => fail.
04. Ambiguous scope or missing ownership => fail.
05. Missing failing doctest, pytest, or explicit acceptance check for changed behavior => fail.
06. `feature` mode without executable failing demo contract before production edits => fail.
07. Feature demo passes before implementation without explicit user override and re-scope note => fail.
08. Symptom-first task edited without `investigate` output or equivalent root-cause evidence => fail.
09. Workaround-only fix presented as completion without explicit temporary-mitigation instruction => fail.
10. Behavior-changing config/agent/skill edit without calibration/routing decision => fail.
11. Specialist-required domain change without specialist output or labeled substitute => fail.
12. Missing `development-notes.md` sections => fail.
13. Detailed workflow: result artifact validator failure => fail.
14. Detailed workflow: result artifact missing => fail.
15. Missing `confidence-calibration.md` sections => fail.
16. Shared confidence policy violation from `../../shared/quality-gates.md` => fail.

## Quality Gates

Required checks:

- `review`: `git diff --check`, changed-file inspection, acceptance criteria trace, simplicity/readability/reproducibility inspection, project docstring-style detection, and docstring/comment policy inspection for changed code.
- `tests`: failing-then-passing check or explicit acceptance probe for changed behavior; `feature` mode must include demo failure before edits and demo pass after implementation.
- `artifact`: shared validator confirms `development-notes.md`, gate logs, and result JSON shape.
- `confidence`: `confidence-calibration.md` and `IMPLEMENT_METADATA.confidence_recovery` satisfy shared confidence band policy from `../../shared/quality-gates.md`.

Conditional checks:

- `lint`/`format`/`types`: run project-configured commands when code or typed config changed.
- `calibration`: run when workflow skills, role/agent routing, `.codex/config.toml`, or calibration fixtures changed; Codex Rig source uses `runtime/calibration/run.py --layout plugin`.

### Coding standards

Advisory standards for changed code, applied during Step 05 and inspected by the `review` gate:

1. Add a purpose docstring to new or materially changed functions/methods in the configured, established, or fallback project style; generated or third-party code stays outside edited ownership. Correct owned documentation automatically rather than failing for its presentation alone.
2. Explain non-trivial new or changed blocks with useful inline comments, or simplify the code until its rationale is clear. Missing commentary alone is an advisory; incorrect behavior or a misleading factual contract still needs correction and verification.
3. Move an explanatory comment directly above a new or changed `def`/`class` into its docstring, preserving meaning. Comments inside the body remain governed by standard 2. Placement alone never withholds verified work.
4. Simplify long, dense, or deeply nested owned blocks when it improves clarity under the project pattern; prefer guard clauses and avoid speculative abstractions. Subjective density alone is an advisory, not an acceptance failure.
5. Remove low-value argument-remapping wrappers when doing so reduces maintenance burden without changing authorized behavior. Keep units that materially improve readability, testability, or API stability; wrapper size or usage count alone never blocks delivery.

**Spike exemption**: standards 1-5 (docstring/structure) do not apply to `spike` mode output that stays a disposable, non-retained probe never merged into the codebase. When a spike is later promoted to retained implementation, standards 1-5 resume in full for the promoted code — the exemption does not carry over.

Apply standards 1-5 through authorized automatic owned corrections and relevant configured-check reruns. Preserve useful documentation, project checks, factual evidence and actual authority boundaries. A remaining presentation preference is advisory; block only when a concrete required structure, behavior, fact or authorization remains unmet under [Acceptance and recovery](../../shared/native-skill-contract.md#acceptance-and-recovery).

## Calibration Hooks

Update calibration when implementation routing or output expectations change:

- benchmark patterns: `implement`
- behavioral cases: symptom-first routing, specialist substitution, config behavior changes, premature portable read-only adoption, missing acceptance probe, feature demo gate bypass, missing project docstring-style detection, missing function docstrings, overlong docstrings masking complex code, long code blocks not factored, deep branching without guard clauses, low-value argument-remapping wrappers, pre-definition comments that should be docstrings, missing explanatory inline comments, low-confidence recovery loop, objective confidence evidence, artifact validator bypass, spike output promoted to retained implementation without resuming docstring/structure rules 15-19

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`. Final chat follows its ordered frame. `Outcome` is `pass`, `fail`, `partial`, or `blocked` and states whether `done_when` was met. When multiple surfaces changed, `Results` uses exactly `Surface | Outcome | Verification | Remaining limit`. Apply shared `Verification`, `Remaining`, `Next steps`, and supplemental `Artifact` rules. `Confidence` also includes band, recovery actions, material gaps/degradation reasons, and closure status from `metadata.confidence_gaps` and `metadata.confidence_gap_closures`.

Minimum artifact payload template: `result-template.json`.

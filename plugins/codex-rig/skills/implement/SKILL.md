---
name: implement
description: Implement changes with a linear plan-build-verify workflow and measurable quality gates.
---

# Implement

See the [fixed recurrence and root-cause policy](../../shared/native-skill-contract.md#recurrence-and-root-cause-policy) and [reasoning-progress escalation policy](../../shared/native-skill-contract.md#reasoning-progress-escalation) for repeated-obstacle handling; record and validate `reasoning-progress.json` before another cycle after an escalation trigger.

Run linear implementation with strict gates.

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

## Parallel Adoption (Portable read-only)

This skill permits only its promoted portable read-only route. Resolve execution precedence from per-invocation `--execution=<mode>`, then `CODEX_RIG_EXECUTION`, then the `auto` default. The default execution mode is `auto`. `auto` selects this route only after this consumer's runtime matrix and promotion; otherwise it resolves safely to `serial`. Every write still requires a frozen plan and exact-digest approval. This route never bypasses consumer promotion, serial parent authority, or write approval.

Follow the [canonical G0–G8 execution flow](../../ARCHITECTURE.md#canonical-g0g8-execution-flow) for the shared gate order and fork outcomes. This consumer's read-only evidence passes are bounded by G0–G5; the parent owns deterministic G6 integration, G7 verification, and G8 verdict/promotion.

### Safe parallel work

The promoted route permits read-only evidence, acceptance, and documentation-impact passes with immutable disjoint context packs and separate outputs. Source, test, documentation, configuration, calibration, cache, generated-output, artifact, and result writes are outside this portable route.

### Required barrier

Apply the shared [host compatibility check](../../shared/specialist-orchestration.md#host-compatibility-before-dispatch) before preparing any child work. Include `read_host` only from verified launcher-supported child controls. Missing or incompatible controls make `auto` resolve serial with a reason; explicit parallel-read stops before dispatch. A plan declaration is not effective-control proof, and post-run validation remains mandatory.

Before any dispatch, freeze the goal, mode, `done_when`, baseline, ownership DAG, context packs, role-card hashes, checks, resource locks, and plan digest. Dispatch at most one fixed dependency-ready wave, then join every terminal handoff before implementation, integration, gates, or acceptance; changed scope requires a new plan.

The frozen `<run-directory>/execution-plan.json` must include exact `consumer_policy` values `consumer_id=implement`, `capability=portable-read-only`, `promotion_status=promoted`, `parent_mutations=serial`, and `canonical_gates=serial`. It must also include `write_policy`: use `parent_writes=planned` with `approval_requirement=exact-plan-digest` when any parent mutation is planned, otherwise `parent_writes=none` with `approval_requirement=not-required`. A planned write requires `<run-directory>/write-approval.json` containing only the exact plan SHA-256, `response=approve`, and `source=explicit-input|user-prompt`.

Before dispatch, run `python PLUGIN_ROOT/shared/parallel_execution.py preflight --consumer implement --plan <run-directory>/execution-plan.json --approval <run-directory>/write-approval.json`; append `--execution=<mode>` only for an explicit invocation value. Omit `--approval` only when the frozen write policy declares no parent writes. A nonzero result stops the route.

After every spawned child reaches a terminal handoff, run `python PLUGIN_ROOT/shared/parallel_execution.py validate-runtime --consumer implement --manifest <run-directory>/execution-manifest.json --plan <run-directory>/execution-plan.json --parent-rollout <authoritative-parent-rollout> --sessions-dir <authoritative-sessions-directory> --run-dir <run-directory> --roles-dir PLUGIN_ROOT/roles`. Require `runtime_promotion_eligible=true`, `consumer_id=implement`, and `write_parallel_eligible=false`. Run the same preflight again after the terminal join and before the first parent mutation. Any plan, approval, consumer, runtime, or join drift stops mutation and requires a new frozen plan plus exact approval.

### Serial parent decisions

The parent owns all implementation, test, and documentation writes; shared files and dependency chains; integration and conflict handling; calibration, artifacts, and result writes; canonical quality gates; verdict; and promotion. Never expand the wave dynamically or dispatch dependent work early.

### Resource conflicts

Declare only validated resource locks such as `git-index`, `cache:<path>`, `generated:<path>`, and `test-env:<name>`. Shared paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.

### Fallback

Unavailable or unsafe fan-out uses equal-gate `serial-fallback` from the same frozen plan with the same quality gates and retained evidence. Never label fallback as parallel or weaken checks because dispatch was unavailable.

### Acceptance

This skill's shared runtime matrix and consumer promotion must remain complete before `auto` selects this route. Acceptance must prove freeze, complete join, truthful execution label, resource compatibility, equal gates, and unchanged serial parent authority.

### Stop rule

Generic parallel writes remain disabled. Stop without dispatch on missing promotion, mutable packs, ownership or resource overlap, sensitive or unproven controls, missing terminal evidence, or an incomplete join; implementation, test, documentation, and all other mutations remain parent-serial.

## Workflow (Exact Commands)

### 01: Create run directory

Run `create_run.py --skill implement` per `../../shared/helper-cli-contract.md`.

### 02: Record baseline diff and branch

Run `git rev-parse --abbrev-ref HEAD` as an argv command and write stdout to `<run-directory>/branch.txt`.

Inspect `python PLUGIN_ROOT/shared/collect_diff.py --help`; collect `working-tree` into `<run-directory>/baseline`.

### 03: Route the change type and define ownership

Modes:

- `feature`: define public behavior, acceptance checks, docs impact, and tests before implementation.
- `fix`: reproduce or cite the failing behavior before editing.
- `refactor`: preserve behavior with characterization tests or an equivalent safety net.
- `config`: inventory references and calibration/routing impact before editing.
- `spike`: read-only or disposable probe; do not present as completed implementation.

Define narrowest reversible change, owners, acceptance. For 3+ steps/design tradeoffs, update plan before edit.

**Structural context (optional)**: select one task-neutral route at the decision point, then invoke the adapter once: `python PLUGIN_ROOT/shared/codemap_adapter.py context --category implementation --query-kind <kind> [--target <qname>] --out <run-directory>/codemap-context.json`. Use `skip` for an exact localized edit with no unresolved structural fact, the matching single route (`central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling`) for one unresolved fact, and `standard` for broad or unknown scope. Map direct, all, or production caller questions to `callers`; use `blast` only for explicitly transitive caller questions. An explicit user or tool request for structural evidence overrides `skip`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with the routing above. Persist the result once here, before step 05 implementation; step 06 specialist fan-out consumes `<run-directory>/codemap-context.json`, never a fresh query.

### 04: Run the anti-rationalization gate before editing

- Existing code and tests for the target surface have been read.
- Failure mode or new behavior is captured by a failing doctest, pytest, or explicit acceptance check.
- Coding changes have a project coding-principles plan from the applicable `AGENTS.md` layers: simple/readable/reproducible structure first, short reusable units without low-value argument-remapping wrappers, guard clauses or early `return`/`yield`/`continue` for invalid or terminal cases, project docstring-style detection, concise purpose docstrings, and inline comments only for non-trivial implementation blocks.
- `feature` mode has a feature demo contract before production edits:
  - simple public API: inline doctest or focused pytest that shows the intended call and result
  - multi-step behavior: minimal example or pytest exercising the user-visible workflow end to end
  - the demo must be automatically executable and must fail against current code for the intended missing behavior
  - if the demo passes before implementation, stop and re-scope; do not silently proceed unless the user explicitly overrides the gate
- Review the demo contract for goal alignment, API shape, missing scenarios, and automatic verifiability before implementation.
- If the task starts from a symptom, failing test, failing CI, flaky behavior, regression, tool/environment error, or unexplained metric shift, run `investigate` first or document equivalent root-cause evidence before editing.
- Root-cause evidence includes the claim, supporting logs/code, a falsification check, and at least one rejected alternative. A workaround-only change is a temporary mitigation, not completion, unless explicitly requested by the user.
- Behavior-preserving refactors have characterization tests or an equivalent current-behavior safety net.
- The next edit is the smallest reversible step, not a speculative refactor.

### 05: Implement minimal change

While implementing, keep the code understandable from the code itself:

- Apply the consolidated project coding principles from the applicable `AGENTS.md` layers.
- Refactor long, dense, or deeply nested blocks into named helpers/classes before adding explanatory text.
- Avoid tiny rarely used helpers that only remap arguments; keep the logic inline, use a local helper, or use `functools.partial` when only binding arguments.
- Match the project's configured or established docstring style, and keep function/class purpose in docstrings rather than comments directly above definitions.
- Refactor instead of writing long docstrings or comments when a block needs a long explanation to be understandable.

### 06: Orchestrate specialists when the change crosses a domain boundary

Read and apply `../../shared/specialist-orchestration.md` only when the task crosses domains, benefits from independent verification, or splits into parallel context packs; do not load it for narrow one-domain implementation in one to three files.

Before spawning or substituting specialists, write `<run-directory>/specialist-plan.md` with one row per planned pass:

| role | trigger | context pack | expected output | mode |
| -- | -- | -- | -- | -- |

Required orchestration patterns:

- public API or architecture: `sw-engineer` for implementation, `qa-specialist` for acceptance matrix, and `doc-scribe` for public docs/docstrings when applicable. Use `solution-architect` only when the user expressly requests Sol or selects that role; it returns a bounded read-only design artifact to the Terra parent/session, which continues and accepts.
- bug fix or regression: `investigate` or equivalent root-cause evidence first, then `sw-engineer` for the fix and `qa-specialist` for failure-before/pass-after proof.
- CI/tooling: `cicd-steward` for workflow behavior and `linting-expert` for ruff/mypy/pre-commit or suppression policy.
- security-sensitive code: the Terra parent/session scopes the risk before implementation and pairs `sw-engineer` with `qa-specialist` as needed. Use read-only `security-auditor` only when the user expressly requests Sol or selects that role; it returns a bounded evidence artifact to the Terra parent/session, which continues and accepts.
- ML/data/research behavior: `data-steward` for data contracts, `scientist` for method/metric validity, `squeezer` for performance claims, plus `qa-specialist` for tensor boundary tests.
- docs-impacting behavior: `doc-scribe` gets only the verified public behavior, API signatures, examples, and migration notes; do not send unrelated implementation details.
- high-risk or broad changes: `challenger` runs after the draft plan or diff to stress-test assumptions and residual risk.

Each specialist context pack must include only relevant files, hunks, logs, and questions. Do not give every specialist the full task history. If specialist fan-out is unavailable, record the in-main substitute in `<run-directory>/specialist-notes.md` and lower confidence when independence mattered.

### 07: Write `<run-directory>/development-notes.md` before running gates

Required sections:

- `Scope`
- `Acceptance Criteria`
- `Evidence`
- `Specialist Policy`
- `Gates`

### 08: Run shared quality gates

Inspect `python PLUGIN_ROOT/shared/run_gates.py --help`, then run all project-relevant gates with explicit commands or skip reasons.

### 09: Review the changed files and the gate output before deciding pass/fail

### 10: Classify findings using `../../shared/severity-map.md`

### 11: Run confidence calibration and recovery before any user-facing output

Write `<run-directory>/confidence-calibration.md` with these sections:

- `Initial Confidence`: starting score and the concrete uncertainty sources.
- `Objective Evidence`: code paths read, tests/checks run, reproduction or acceptance evidence, and artifacts inspected.
- `Confidence Gaps`: missing evidence, unverified assumptions, risky substitutions, or unavailable checks.
- `Recovery Actions`: internal loops already performed to increase confidence, such as reading more source, running focused checks, adding/adjusting tests, consulting specialist policy, or reducing scope.
- `Recomputed Confidence`: final score after recovery, with why it is objectively supported.
- `Remaining Limits`: residual uncertainty and why it is acceptable or blocking.

Shared confidence policy:

Apply the shared confidence band policy from `../../shared/quality-gates.md`. This skill records the required evidence in `confidence-calibration.md` and mirrors it in `IMPLEMENT_METADATA.confidence_recovery` before output.

Confidence must be honest and objectively verifiable. Do not inflate it to pass a gate; if the evidence is missing, keep the lower score and fail or time out with the missing evidence named.

### 12: Write and validate the mandatory result artifact

Follow `../../shared/helper-cli-contract.md` and authoritative help. Write with `IMPLEMENT_METADATA`, validate as skill `implement`, and promote only the validated candidate.

`IMPLEMENT_METADATA.confidence_recovery` must mirror `confidence-calibration.md` and include `initial_confidence`, `final_confidence`, `status`, `evidence`, `recovery_actions`, and `remaining_limits`. `IMPLEMENT_METADATA.confidence_gap_closures` must include one closure record per non-empty `confidence_gaps` entry, with `status=closed|unresolved|deferred` and matching evidence or rationale.

## Fail-fast Rules

01. Missing `goal` or `done_when` => fail.
02. Shared gate script missing => fail.
03. Any critical finding => fail.
04. Ambiguous scope or missing ownership => fail.
05. Missing failing doctest, pytest, or explicit acceptance check for changed behavior => fail.
06. `feature` mode without an executable failing demo contract before production edits => fail.
07. Feature demo passes before implementation without explicit user override and re-scope note => fail.
08. Symptom-first task edited without `investigate` output or equivalent root-cause evidence => fail.
09. Workaround-only fix presented as completion without explicit temporary-mitigation instruction => fail.
10. Behavior-changing config/agent/skill edit without calibration/routing decision => fail.
11. Specialist-required domain change without specialist output or labeled substitute => fail.
12. Missing `development-notes.md` sections => fail.
13. Result artifact validator failure => fail.
14. Result artifact missing => fail.
15. New or materially changed function/method without a purpose docstring in the configured, established, or fallback project style => fail unless it is generated or third-party code explicitly outside the edited ownership.
16. Non-trivial new or changed code block without an explanatory inline comment => fail unless the code was refactored until the rationale is obvious from names and structure.
17. Explanatory inline comment immediately before a new or changed function/class definition => fail; move that explanation into the docstring.
18. Long, dense, or deeply nested new/changed code block that could be split into clear helpers/classes or simplified with guard clauses => fail unless the local project pattern requires the structure.
19. Low-value tiny function/class that only remaps arguments, wraps one call without a semantic purpose, or is rarely used => fail unless it materially improves readability, testability, or API stability.
20. Missing `confidence-calibration.md` sections => fail.
21. Shared confidence policy violation from `../../shared/quality-gates.md` => fail.

## Quality Gates

Required checks:

- `review`: `git diff --check`, changed-file inspection, acceptance criteria trace, simplicity/readability/reproducibility inspection, project docstring-style detection, and docstring/comment policy inspection for changed code.
- `tests`: failing-then-passing check or explicit acceptance probe for changed behavior; `feature` mode must include the demo failure before edits and demo pass after implementation.
- `artifact`: shared validator confirms `development-notes.md`, gate logs, and result JSON shape.
- `confidence`: `confidence-calibration.md` and `IMPLEMENT_METADATA.confidence_recovery` satisfy the shared confidence band policy from `../../shared/quality-gates.md`.

Conditional checks:

- `lint`/`format`/`types`: run project-configured commands when code or typed config changed.
- `calibration`: run when workflow skills, role/agent routing, `.codex/config.toml`, or calibration fixtures changed; Codex Rig source uses `runtime/calibration/run.py --layout plugin`.

## Calibration Hooks

Update calibration when implementation routing or output expectations change:

- benchmark patterns: `implement`
- behavioral cases: symptom-first routing, specialist substitution, config behavior changes, premature portable read-only adoption, missing acceptance probe, feature demo gate bypass, missing project docstring-style detection, missing function docstrings, overlong docstrings masking complex code, long code blocks not factored, deep branching without guard clauses, low-value argument-remapping wrappers, pre-definition comments that should be docstrings, missing explanatory inline comments, low-confidence recovery loop, objective confidence evidence, artifact validator bypass

## Output Contract

Before writing the result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`. Final chat follows its ordered frame. `Outcome` is `pass`, `fail`, `partial`, or `blocked` and states whether `done_when` was met. When multiple surfaces changed, `Results` uses exactly `Surface | Outcome | Verification | Remaining limit`. Apply the shared `Verification`, `Remaining`, `Next steps`, and supplemental `Artifact` rules. `Confidence` also includes band, recovery actions, material gaps/degradation reasons, and closure status from `metadata.confidence_gaps` and `metadata.confidence_gap_closures`.

Minimum artifact payload template: `result-template.json`.

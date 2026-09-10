---
name: calibrate
description: Calibrate skills/role cards for leaks/gaps with recall, precision, and confidence-accuracy checks.
---

# Calibrate

Run calibration for Codex workflow integrity and behavioral scoring.

## Input Schema

```json
{
  "scope": "skills|agents|routing|all",
  "pace": "fast|full",
  "mode": "ab-test|apply",
  "require_live_routes": false,
  "skip_gate": false,
  "done_when": "recall and bias scores emitted; proposals written if mode=apply; gate skipped if skip_gate=true"
}
```

## Workflow

Installed plugin runs use `--layout plugin --root <consuming-project>`. The runner discovers package assets from its own file location under `runtime/calibration`, `skills`, `roles`, and `shared`; `--root` controls only report output, Git context, and read-only classification work. It must not fall back to source checkout or project `.codex`.

Repository maintainers may use `--layout source --root <source-project>` to validate source `.codex` layout. Do not mix source agents, sync manifests, or project registration checks into installed-plugin result.

### 01: Load calibration task set from `../../runtime/calibration/tasks.json`

### 02: Load behavioral cases from `../../runtime/calibration/behavioral-cases.json`

### 03: Load behavioral observations from `../../runtime/calibration/behavioral-observations.jsonl`

- Require `source`, `run_id`, `observed_at`. `source=live-*` also needs route; campaign/pair IDs; pair/registered role; actual model/effort; recomputable prompt/task-contract SHA-256; task type/scope; input/cached/output tokens; latency; outcome; tool/check failures; normalized cost; pricing reference. Each complete campaign exactly matches case/role/type/scope signatures in `live-ab-tasks.json`; substituted task, fixture, gate, prompt input fails.

### 04: Inspect `../../runtime/calibration/run.py --help`, then run plugin layout against the consuming project

Use `--require-live-routes` only for strict-live gate. Default offline scoring remains fixture-backed and makes no paid model calls.

### 05: Inspect `checks_failed`, `leaks_found`, and `behavioral`

### 06: Review behavioral metrics:

- `recall`: expected IDs recovered from known cases.
- `precision`: reported IDs matching expected IDs.
- `confidence_accuracy`: `1 - mean(abs(confidence - per-case F1))`.
- `mean_overconfidence`: mean positive confidence bias over per-case F1.
- `gate_metrics_raw`: unrounded pass/fail values.
- `by_source`: recall, precision, confidence calibration by source.
- `observation_freshness`: latest `observed_at`, missing timestamps, live/fixture counts.
- `live_route_acceptance`: matched baseline/candidate classification and isolated tool-use quality, normalized token-efficiency proxy, evidence sufficiency per configured route; not monetary pricing evidence.

### 07: Classify gaps as blocking or non-blocking

### 08: Emit measured recommendations for what should be fixed or improved next

- Start with plain-English explanation of whether calibration passed and what any failure means. Then prioritize failed checks/leaks, naming exact check, file or pattern, evidence, next-action owner, and gate that must pass to resume acceptance.
- Behavioral recommendations name metric gap/affected cases when available.
- Separate fixture-only caveats from live-quality claims.

### 09: Write skill artifacts to `.reports/codex/calibrate/<timestamp>/`; preserve runner evidence under `.reports/codex/calibration/<timestamp>/`

### 10: Write the validated skill-level artifact when this skill wraps the runner

Follow `../../shared/helper-cli-contract.md`/authoritative help. Gate intent: ruff lint/format calibration+skills, explicit no-typed-target reason, calibration tests, clean diff. Write `CALIBRATE_METADATA`, validate `calibrate`, promote only validated candidate.

## Native Contract Checks

Verify configured native surface, not only runner internals.

Skill checks:

- configured skill file exists; frontmatter has unindented `---`, `name:`, `description:`; required sections exist; artifact path `.reports/codex/<skill>/`; examples include `status`, `checks_run`, `checks_failed`, `findings`, `confidence`, `artifact_path`; no external runner-only metadata/cache.
- CLI checks find every local shebang Python/shell entry point in calibration, shared helpers, code-review, offline harness; each executable, fixed-help-roster registered, authoritative `--help`.
- every skill references `helper-cli-contract.md`, not complete local CLI invocations.
- source layout compares `../../runtime/calibration/behavioral-cases.json` version to `HEAD`: dirty tree same or exactly one commit-relative version step; installed plugin layout records packaged fixture as immutable.

Role checks:

- installed layout requires every packaged `roles/<role>/ROLE.md`; source layout requires each configured source agent.
- role-card frontmatter contains role ID, namespaced name, active model, reasoning effort, approval policy, sandbox, and fallback modes; package-manifest skill/role rosters contain every calibrated target.
- default, review parent, runtime, research, curation, adversarial use `gpt-5.6-terra`; delegation/docs/CI-CD/web/OSS/static analysis use `gpt-5.6-luna`; only security/solution architecture use `gpt-5.6-sol`.
- Luna/high is explicit human override for bounded simpler roles; preserve strict quality/cost failure, reject undocumented expansion.
- every role defaults `high`; `xhigh`/`max` explicit task escalation. `model_reasoning_effort` follows agent-effort-policy: all `high`, `xhigh`/`max` task overrides.
- high-stakes roles use high-capability tier; bounded support may lower-cost tier. No deprecated model string in active config/TOML.
- role has clear trigger/skip/not-for boundaries, evidence ownership, execution constraints, handover, and confidence contracts; sensitive roles retain sandbox, especially read-only security audit; packaged roles require no external runtime path variable.

## Usage Notes

- After meaningful agent/skill instruction change, confirm routing/output match stack.
- `leaks_found` primary drift; `checks_failed` mechanical gate.
- Behavioral metrics measure supplied observations only. `fixture-selftest` validates scoring; live Codex quality requires replacing/appending live-prompt observations.
- Missing route coverage is `insufficient-evidence`, never acceptance; `require_live_routes=true` exits nonzero.
- Compare thresholds with `gate_metrics_raw`, not rounded display.
- Paid paired campaigns: `../../runtime/calibration/run_live_ab.py`; plans by default, executes only `--confirm-paid-run=chatgpt-subscription`, verified local ChatGPT subscription login, no API key env, no `CI`/`GITHUB_ACTIONS`. An executing campaign applies full networked CLI approval and denial contract in `../../shared/native-skill-contract.md` to complete owning command because it spawns `codex exec`. The operation-specific brief is: `Action and purpose`: run confirmed paid paired calibration; `External capability`: paid ChatGPT subscription execution through `codex exec`; `Credential behavior`: use verified local ChatGPT subscription login without reading API keys or credentials; `Filesystem and worktree effects`: write calibration artifacts only to selected run directory; `Retry policy and safe denial outcome`: stop turn on denial and retain sandboxed planning or offline scoring only. Planning and offline scoring remain sandboxed.
- Each live task names canonical role. Plugin layout prepends exact packaged role card to both prompts; source layout preserves project-instruction plus source-agent prompt construction. Tool pairs can accept candidate passing executable gate when successfully invoked baseline fails; infrastructure timeout is never candidate win.
- Sol critical-only unless paired quality exceeds Terra configured minimum; tie retains Terra.
- Do not claim currency savings from `normalized-token-v1`; need dated authoritative model-specific price.
- Fixture `version` is committed-history marker: compare `git show HEAD:<path>`; dirty tree stays committed or one-next version until commit.
- Missing registration/pattern mismatch: inspect named file and expected registration or pattern first; record observed mismatch. Apply smallest evidenced correction only within authorized edit scope, then rerun that failed check before widening. Otherwise ask for exact missing file, scope approval, or owner decision; never offer only "fix configuration and retry".

## Fail-Fast Rules

1. Missing calibration files => fail.
2. Missing configured skill or role file => fail.
3. Native skill/role contract mismatch => fail unless result waives.
4. Runtime leakage in native skill or role files => fail.
5. Behavioral gate below threshold => fail.
6. Result artifact missing => fail.
7. Behavioral case-set version >1 step from committed version => fail.
8. `require_live_routes=true` with incomplete route pairs => fail.
9. Live row without strict paired execution schema => fail.

## Quality Gates

Required checks:

- `calibration`: `../../runtime/calibration/run.py --layout plugin --root <consuming-project>`.
- `behavioral-version-policy`: compare case-set version to `HEAD`; avoid meaningless dirty-tree gaps.
- `review`: inspect failed patterns, leaks, behavioral gaps, stale fixtures before recommendations.

Conditional checks:

- `tests`: run focused tests when calibration code changes.
- `format`: validate JSON and shell syntax when calibration fixtures change.

## Calibration Hooks

When calibration expectations change, update together:

- `../../runtime/calibration/benchmarks.json`
- `../../runtime/calibration/behavioral-cases.json`
- `../../runtime/calibration/behavioral-observations.jsonl`
- `../../runtime/calibration/run.py`
- `../../runtime/calibration/live-route-policy.json`
- `../../runtime/calibration/live-ab-tasks.json`
- `../../runtime/calibration/run_live_ab.py`

Behavioral coverage includes networked CLI owning-command approval for paid live execution.

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared ordered frame. `Outcome` is `pass`, `fail`, or `insufficient-evidence`. `Results` has one measured check or metric per row and exactly `Check / metric | Result | Evidence | Next action`. Apply shared `Verification`, `Remaining`, `Next steps`, `Confidence`, and supplemental `Artifact` rules; include runner mode/coverage, every failed/skipped/deferred check, and calibration recovery evidence.

Minimum artifact payload template: `result-template.json`.

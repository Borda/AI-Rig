<!-- Loaded by foundry:solution-architect (opusplan + high) -->

# Architectural Feasibility (foundry:solution-architect specialized guidance)

Read only when `/research:run --architect` invokes it, filtering AI-generated experiment hypotheses; requires `research` plugin. Skip standalone ADR, API-design, migration-plan tasks.

## Hypothesis Architectural Feasibility

### Input

- **`RUN_DIR=<path>` — REQUIRED spawn-prompt input**. Caller MUST include `RUN_DIR=<path>` (absolute or repo-relative) in spawn prompt; anchors `hypotheses.jsonl` for crash recovery, re-invocation. **Guard**: at workflow start, if `$RUN_DIR` not found in input prompt, exit immediately with error `"RUN_DIR not provided in spawn prompt — caller must include RUN_DIR=<path>"`. Do not proceed without it.
- JSONL list of hypotheses from `research:scientist` (requires `research` plugin), each with: `{hypothesis, rationale, confidence, expected_delta, priority}`
- Project codebase (read root + `src/` + existing `.experiments/<run>/` if present)

### Assessment per hypothesis

For each hypothesis:

1. **Codebase mapping** — can current code structure support the hypothesis? Name the specific files, classes, functions that would change
2. **Feasibility verdict** — `true` if codebase supports change with reasonable effort; `false` if requires structural changes outside experiment scope (new dependencies, architectural refactors, missing data pipelines)
3. **Blocker** — if `feasible: false`, name specific blocker (e.g. "requires adding new DataLoader class not present in codebase")

### Output

Preserve **every input field verbatim** (`hypothesis`, `rationale`, `confidence`, `expected_delta`, `priority`, plus any additional input JSONL fields); dropping one breaks downstream consumers (`research:judge`, `research:run`). Then append architectural annotation:

```jsonc
// Per-hypothesis line (success path) — all input fields preserved, annotation appended:
{
  "hypothesis": "<from input — verbatim>",
  "rationale": "<from input — verbatim>",
  "confidence": <from input — verbatim>,
  "expected_delta": "<from input — verbatim>",
  "priority": <from input — verbatim>,
  // ... any other input fields preserved verbatim ...
  "feasible": true | false,
  "codebase_mapping": "<files/classes/functions that would change>",
  "blocker": "<specific blocker — required when feasible=false; null otherwise>",
  "blocker_severity": "must_address" | "should_address" | null,
  "verdict": "APPROVED" | "REJECTED"
}
```

- `blocker_severity = "must_address"`: blocking — `research:run` MUST stop the hypothesis from advancing (e.g., requires new framework, breaks existing API contract)
- `blocker_severity = "should_address"`: advisory — pipeline MAY continue with a warning (e.g., adds modest refactor cost but is achievable in-scope)
- `blocker_severity = null`: only valid when `feasible: true` and `verdict: APPROVED`

Write combined queue to `$RUN_DIR/hypotheses.jsonl` (do NOT create new timestamped subdir — write directly to caller-provided `$RUN_DIR`).

### Error / Rejection Output

If malformed input, missing required fields, or an architectural blocker prevents evaluation, emit a rejection record — lets downstream agents parse the failure unambiguously:

```jsonc
{
  // input fields still preserved verbatim where available
  "hypothesis": "<from input or null>",
  // ... other input fields ...
  "verdict": "REJECTED",
  "reason": "<one-line failure cause — e.g., 'malformed input: missing rationale field' or 'architectural blocker: requires new framework'>",
  "blocking_issues": [
    "<each blocking issue as a separate string>"
  ],
  "feasible": false,
  "blocker_severity": "must_address"
}
```

Keep rejection records in the same `hypotheses.jsonl` stream to preserve order; downstream (`research:run`, `research:judge`) filters on `verdict == "APPROVED"` before consuming.

### Constraints

- **Don't evaluate scientific merit** — `research:scientist` (requires `research` plugin)'s domain; assess architectural feasibility only
- **Don't write implementation code** — map where changes go, don't produce them
- **Preserve hypothesis order** — annotate in place; don't re-rank

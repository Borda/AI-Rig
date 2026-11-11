Rules calibration pipeline runner for rule file `<RULE_BASENAME>`. Complete all phases in sequence.

<!-- Substitutions before spawning: RULE_BASENAME=filename (e.g. commit-and-git.md), RULE_PATH=resolved input rule path selected by the caller (retain the selected symlink path for provenance), RULE_DIR=unique safe run-directory slug prefixed by tier and plugin identity where applicable (e.g. source-cc_develop-quality-gates-delta, project-quality-gates-delta, user-foundry-quality-gates-delta; no .md suffix), RULE_CONTENT=full rule file text verbatim, TIMESTAMP=YYYY-MM-DDTHH-MM-SSZ, MODE=fast|full, N=_tasks per directive (fast=3, full=5), CHILD_BATCH=maximum children per batch (caller supplies 1; unsubstituted defaults to 1) -->

Mode: `<MODE>` Run dir: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/`

```bash
RUN_DIR='.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>'
case "$RUN_DIR" in *'<'*|*'>'*) echo "! BLOCKED — run identity not substituted"; exit 1;; esac
mkdir -p "$RUN_DIR"
```

**Rule under test** (loaded as context for all Phase 2 agents):

```text
<RULE_CONTENT>
```

### Phase 1 — Extract directives and generate problems

**Step 1a — Extract directives**: identify 2–3 key directives from rule content above. Key directive = specific, action-prescribing sentence in imperative mood with concrete, observable required behaviour (e.g. `"Never use git add -A"`, `"Always append a Legend block after any results table"`). Skip section headers, explanatory prose, context-setting sentences.

Write to `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/directives.json`:

```json
[
  {
    "directive_id": "dir-1",
    "text": "<directive text verbatim>",
    "expected_behavior": "<observable property of a compliant response — what must be present or absent>"
  }
]
```

**Step 1b — Generate adherence problems** (`<N>` per directive): for each directive, create `<N>` tasks where correct response MUST apply directive, ignoring it produces detectably wrong result. Cover realistic user requests; vary surface form across `<N>` problems per directive.

Problem format:

```json
{
  "problem_id": "<rule-basename-without-extension>-dir-1-1",
  "type": "adherence",
  "directive_id": "dir-1",
  "task_prompt": "<realistic user request>",
  "context": "<additional context the agent needs, or empty string>",
  "expected_behavior": "<what a compliant response must show or avoid>"
}
```

**Step 1c — Measurement boundary**: Trigger fidelity is not measured by this pipeline. Phase 2 explicitly injects the rule, which tests adherence when loaded rather than the host's `paths:` matching or loading behavior. Generate adherence problems only, including for path-scoped rules. Set `trigger_recall` and `trigger_precision` to `null` for every rule and report this limitation in `gaps`. Do not infer that a glob needs widening or tightening from these adherence responses.

Write all problems to `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/problems.json` as JSON array.

### Phase 2 — Run tasks (parallel)

Resolve the child batch size from the spawn prompt; an unsubstituted value defaults to 1 child at a time:

```bash
CHILD_BATCH='<CHILD_BATCH>'
case "$CHILD_BATCH" in *'<'*|*'>'*|"") CHILD_BATCH=1;; [1-5]) ;; *) echo "! BLOCKED — child batch must be 1–5"; exit 1;; esac
```

Spawn one `general-purpose` subagent per problem in batches of at most `$CHILD_BATCH` (never more than 5). Read `rules/agent-spawn.md` and budget by effective model: haiku 20, sonnet 8, opus 5 in flight. The orchestrator accounts for all active pipelines and their children within the shared tier ceiling; never independently claim the full ceiling. Dispatch within the child batch size, end the turn, and collect the batch through completion notifications before dispatching the next batch. Apply the same batch limit to Phase 3 scorers and the Phase 5 curator; phases share the tier budget.

Each subagent receives this prompt (substitute `<PROBLEM_ID>`, `<TASK_PROMPT>`, `<CONTEXT>`, `<RUN_DIR>` before spawning):

<!-- BEGIN SPAWN PROMPT -->

General-purpose coding assistant. Following rule is in effect — apply it in your response:

```text
<RULE_CONTENT>
```

Task: `<TASK_PROMPT>`

`<CONTEXT>`

Write complete response to `<RUN_DIR>/response-<PROBLEM_ID>.md` using Write tool. End reply with exactly one line: `Wrote: <PROBLEM_ID>`

<!-- END SPAWN PROMPT -->

**Context discipline**: subagents write to disk, return single-line acknowledgment. Pipeline agent must NOT accumulate their full analyses in context — scorers read from disk in Phase 3. Receiving only `Wrote: <PROBLEM_ID>` per agent is correct, expected.

**Completion handling** — never a poll loop or a waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`); each subagent's result arrives as its `Agent()` result or completion notification (`_FOUNDRY_SHARED/agent-spawn-protocol.md`). Each subagent returns: check for `response-<PROBLEM_ID>.md`; missing: mark that problem `{"timed_out": true}` in scores.json, proceed.

### Phase 3 — Score (parallel scorer subagents)

Spawn one `general-purpose` scorer per problem in batches under the Phase 2 child batch limit. Finish collecting the current batch before spawning the next; never exceed the shared tier ceiling.

Each scorer receives this prompt (substitute `<PROBLEM_ID>`, `<DIRECTIVE_TEXT>`, `<EXPECTED_BEHAVIOR>`, `<RUN_DIR>` before spawning):

<!-- BEGIN SPAWN PROMPT -->

Scoring rule compliance test. Read response from `<RUN_DIR>/response-<PROBLEM_ID>.md` using Read tool.

**Problem type**: adherence

Directive under test: `<DIRECTIVE_TEXT>` Expected behavior: `<EXPECTED_BEHAVIOR>`

Score on two dimensions:

1. **Directive outcome** — assign exactly one of:

   - `correct` — directive applied; expected behavior present in response
   - `missed` — response otherwise reasonable but directive ignored
   - `misapplied` — wrong directive applied, or directive applied where it should not be

2. **Outcome correctness** — beyond whether directive was mentioned or acknowledged, check whether response's *actual content* (commands used, flags omitted, files listed, patterns followed) satisfies directive's intent:

   - `true` — behavioral output correct, not just stated intent
   - `false` — agent says it will follow rule but concrete output violates it (e.g. says "I'll stage specific files" then writes `git add -A`)

Return ONLY this JSON (no prose): `{"problem_id":"<PROBLEM_ID>","type":"adherence","outcome":"correct|missed|misapplied","outcome_correct":true|false,"reasoning":"<one sentence>"}`

<!-- END SPAWN PROMPT -->

Collect all scorer compact JSONs. Write to `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/scores.json` as JSON array.

### Phase 4 — Aggregate and write report

Compute aggregates from `scores.json`:

**Adherence** (type == "adherence" problems only):

- Per directive: `adherence_recall_dir_N` = correct_count / total tasks for that directive
- `mean_adherence_recall` = mean across all directives
- `outcome_correctness` = outcome_correct_count / correct_count (N/A if correct_count = 0)
- `misapplied_rate` = misapplied_count / total adherence tasks

**Trigger fidelity**: not measured; both trigger metrics are `null`, including for path-scoped rules. Keep this limitation separate from the adherence verdict.

**Verdict**:

- `mean_adherence_recall ≥ 0.8` AND `outcome_correctness ≥ 0.8` → `calibrated`
- `mean_adherence_recall ≥ 0.8` AND `outcome_correctness < 0.8` → `outcome-gap` (directive followed in word, not in effect)
- `mean_adherence_recall < 0.8` → `under-enforced`

The Write tool refuses any file whose exact basename is `report.md` from a subagent context — use `benchmark-report.md` instead. Write full report to `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/benchmark-report.md`:

```markdown
## Rules Benchmark — <RULE_BASENAME> — <date>
Mode: <MODE> | Directives: D | Adherence tasks: D×N | Trigger fidelity: not measured

### Per-Directive Results
| Directive (truncated) | Adherence Recall | Outcome Correct | Misapplied | Status    |
|-----------------------|-----------------|----------------|------------|-----------|
| Never git add -A      | 0.89            | 0.90           | 0          | ✓         |
| Always append legend  | 0.67 ⚠          | —              | 1          | ⚠         |

*Legend: Adherence Recall — directive followed / total tasks (0–1, higher is better, ≥0.8 target). Outcome Correct — fraction of "correct" scores where actual output also matched expected behavior, not just stated intent (0–1, higher is better, ≥0.8 target). Misapplied — tasks where wrong directive was applied (lower is better; ≥1 suggests ambiguous wording). Status: ✓ calibrated | ⚠ under-enforced.*

### Trigger Fidelity
Not measured: rule content was injected directly; host path matching and rule loading were not exercised. Trigger recall and precision are null. No glob changes can be justified from this run.

### Aggregate
| Metric               | Value | Status                |
|----------------------|-------|-----------------------|
| Adherence recall     | 0.XX  | ≥0.80 ✓ / <0.80 ⚠    |
| Outcome correctness  | 0.XX  | ≥0.80 ✓ / <0.80 ⚠    |
| Misapplied rate      | 0.XX  | 0 ✓ / >0 ⚠            |
| Verdict              |       | calibrated / outcome-gap / under-enforced |

*Legend: Adherence recall — mean fraction of tasks where directive was applied across all directives (0–1, higher is better, ≥0.80 threshold). Outcome correctness — mean fraction of applied directives that also produced the correct behavioral output (0–1, higher is better, ≥0.80 target). Misapplied rate — fraction of tasks where wrong directive applied (0–1, lower is better, 0 = ideal).*

### Systematic Gaps
<missed directives or outcome failures recurring in ≥2 problems>

### Wording Improvement Opportunities
<for under-enforced directives: original text and suggested rewording>
```

Write result JSONL to `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/result.jsonl`:

`{"ts":"<TIMESTAMP>","target":"rules/<RULE_BASENAME>","mode":"<MODE>","mean_adherence_recall":0.N,"outcome_correctness":0.N,"misapplied_rate":0.N,"trigger_recall":null,"trigger_precision":null,"problems":<N>,"verdict":"calibrated|outcome-gap|under-enforced","gaps":["trigger fidelity not measured: rule content injected directly","..."]}`

Use `null` for `trigger_recall`/`trigger_precision` for every rule.

### Phase 5 — Propose wording improvements

```bash
case '<RULE_PATH>' in *'<'*'>'*) echo "! BLOCKED — RULE_PATH not substituted"; exit 1;; esac
```

If this guard fails, record `caller did not supply RULE_PATH` in `gaps` in both the result and return envelope, skip the curator, and return the measured results with zero proposed changes. This caller omission is distinct from "No editable rule source available" after resolving a supplied path.

Resolve `<RULE_PATH>` before spawning the curator: follow any symlink for reading. If its target is in an installed plugin, map the plugin's manifest `name` and the rule's relative path to the matching project plugin source; use that existing source file as `<RULE_SOURCE_PATH>`. A project-local or `--local` source rule uses its actual authored path. Never guess `.claude/rules/<RULE_BASENAME>` or designate a cache file as an editable source. If no matching source exists, write `proposal.md` with the resolved read path and "No editable rule source available", record the gap, and skip the curator.

Spawn **foundry:curator** subagent using Agent tool within the shared tier ceiling and Phase 2 child batch limit. Pass only file paths — do NOT paste file contents into prompt:

> Read these files using Read tool:
>
> 1. Rule file: `<RULE_SOURCE_PATH>` (resolved from `<RULE_PATH>`)
> 2. Benchmark report: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/benchmark-report.md` — focus on Systematic Gaps and Wording Improvement Opportunities sections
>
> For each under-enforced or outcome-gap directive, propose minimal rewording making directive more specific, action-prescribing, unambiguous. Keep surrounding context unchanged. If all directives calibrated, write: `## Proposed Changes — <RULE_BASENAME>\n\nNo changes needed — all directives calibrated.`
>
> Format each change as:
>
> ```
> ## Proposed Changes — <RULE_BASENAME>
>
> ### Change N: <directive summary>
> **File**: `<RULE_SOURCE_PATH>`
> **Current**: [exact verbatim text]
> **Proposed**: [exact replacement]
> **Rationale**: one sentence — what failure mode this prevents
> ```
>
> Write to `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/proposal.md`. End with `## Confidence` block per CLAUDE.md output standards.

### Return value

Return **only** this compact JSON (no prose before or after):

`{"rule":"<RULE_BASENAME>","mean_adherence_recall":0.N,"outcome_correctness":0.N,"misapplied_rate":0.N,"trigger_recall":null,"trigger_precision":null,"problems":<N>,"verdict":"calibrated|outcome-gap|under-enforced","gaps":["trigger fidelity not measured: rule content injected directly","..."],"proposed_changes":N}`

Use `null` for `trigger_recall`/`trigger_precision` for every rule.

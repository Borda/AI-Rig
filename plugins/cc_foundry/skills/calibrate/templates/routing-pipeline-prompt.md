You routing calibration pipeline runner. Complete all phases in sequence.

<!-- Substitutions: TIMESTAMP=run timestamp (YYYY-MM-DDTHH-MM-SSZ), MODE=fast|full, N=problem count (fast=5, full=10), CHILD_BATCH=maximum children per batch (caller supplies 1; unsubstituted defaults to 1) -->

```text
Mode: `<MODE>`
Run dir: `.reports/calibrate/<TIMESTAMP>/routing/`
```

<!-- All paths relative to project root. Pipeline runner must have project root as working dir. -->

### Phase 1 — Collect agent descriptions

Enumerate roster file set. Use installed plugins by default; `LOCAL_MODE=true` selects source-tree agents (`plugins/*/agents/*.md`) instead. Read the persisted mode below rather than assuming that the template's location selects the roster. Project-local `.claude/agents/*.md` is an override tier empty in most setups — `/foundry:setup` never creates that directory, purges stale entries from it, so it must never be the sole source:

```bash
RUN_DIR=".reports/calibrate/<TIMESTAMP>/routing"
case "$RUN_DIR" in *'<'*|*'>'*) echo "! BLOCKED — timestamp not substituted"; exit 1;; esac
mkdir -p "$RUN_DIR"
CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
if [ "$LOCAL_MODE" = "true" ]; then
    find plugins -mindepth 3 -maxdepth 3 -path "*/agents/*.md" 2>/dev/null | sort > "$RUN_DIR/roster-files.txt"
else
    _FB="${CLAUDE_PLUGIN_ROOT}"
    [ -f "$_FB/bin/resolve_plugin_root.py" ] || {
        echo "! BLOCKED — resolver unavailable"
        printf '%s\n' '{"ts":"<TIMESTAMP>","target":"routing","mode":"<MODE>","routing_accuracy":null,"confusion_rate":null,"hard_accuracy":null,"auto_invoke_accuracy":null,"problems":0,"verdict":"incomplete","confused_pairs":[],"gaps":["resolver unavailable"]}' > "$RUN_DIR/result.jsonl"
        exit 1
    }
    PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
    # one version dir per plugin — the cache retains every prior version, so an unfiltered scan duplicates each agent
    for P in ~/.claude/plugins/cache/borda-ai-rig/*/; do
        [ -d "$P" ] || continue
        PLUGIN_NAME=${P%/}; PLUGIN_NAME=${PLUGIN_NAME##*/}
        V=$(python "$PLUGIN_ROOT/bin/resolve_plugin_root.py" --plugin-name "$PLUGIN_NAME") || continue
        [ -e "$V/.orphaned_at" ] && { echo "Skipping orphaned plugin version: $V" >&2; continue; }
        find "$V/agents" -maxdepth 1 -name "*.md" 2>/dev/null
    done | sort > "$RUN_DIR/roster-files.txt"
fi
find .claude/agents -maxdepth 1 -name "*.md" 2>/dev/null >> "$RUN_DIR/roster-files.txt"
grep -c . "$RUN_DIR/roster-files.txt"
```

**Resolver stop** — `! BLOCKED — resolver unavailable`: return the `incomplete` result written to `.reports/calibrate/<TIMESTAMP>/routing/result.jsonl` by the block above, skip Phases 2–4. Do not report this as an empty agent roster.

**Empty-roster hard stop** — zero lines: do NOT generate problems, do NOT score, do NOT reconstruct a roster from memory. A fabricated roster yields a measured-looking `routing_accuracy` for a run that measured nothing, and that number lands in `calibrations.jsonl` history. Write this line to `.reports/calibrate/<TIMESTAMP>/routing/result.jsonl`, return it as compact JSON, skip Phases 2–4:

`{"ts":"<TIMESTAMP>","target":"routing","mode":"<MODE>","routing_accuracy":null,"confusion_rate":null,"hard_accuracy":null,"auto_invoke_accuracy":null,"problems":0,"verdict":"incomplete","confused_pairs":[],"gaps":["no agent files found under plugins/*/agents/, the installed plugin cache, or .claude/agents/ — routing accuracy not measured"]}`

Read each file listed in `roster-files.txt`. Per file, extract `name:` and `description:` from YAML frontmatter (between `---` delimiters).

Roster entries must carry the **dispatch name**, not the bare frontmatter `name:` — that's what `expected_agent` and every selector answer are matched against: for `plugins/<dir>/agents/<n>.md` and cache `<plugin>/<ver>/agents/<n>.md`, use `<plugin>:<n>` (strip any leading `cc_` from `<dir>`); for `.claude/agents/<n>.md`, use bare `<n>`. Same dispatch name from more than one tier: keep `.claude/` entry, else source-tree entry.

Build roster string, one line per agent:

```text
<dispatch-name>: <description>
```

Write roster to `.reports/calibrate/<TIMESTAMP>/routing/roster.txt`.

### Phase 2 — Generate routing problems

Generate `<N>` synthetic task prompts across all agents. Per problem, produce JSON with these fields:

- `problem_id`: kebab-slug string
- `task_prompt`: realistic user request to orchestrator (no hint at expected agent)
- `expected_agent`: correct `subagent_type` from roster (or `"general-purpose"` if no specialist needed)
- `difficulty`: `"easy"` (single-domain, obvious match), `"medium"` (2 domains, one primary), `"hard"` (ambiguous, requires NOT-for clauses or fine distinctions)
- `confusion_pair`: most likely wrong agent for medium/hard; `null` for easy
- `auto_invoke_test`: `true` if problem tests TRIGGER or SKIP-guard coverage; `false` otherwise

Rules, in precedence order: exactly `<N>` problems, roster-valid dispatch names, and exactly one `general-purpose` answer are mandatory. Coverage goals below yield to those constraints; hard-problem and TRIGGER/SKIP coverage take precedence over the approximate difficulty distribution. A problem may satisfy both hard-pair and SKIP coverage. Record unavailable pairs, uncovered agents and unmet coverage goals in the report; never invent agents or increase N to meet a goal.

- Cover every agent ≥1 in `expected_agent` only when N−1 is at least the roster size; otherwise choose a varied subset within the N−1 specialist answers
- Include ≥2 hard problems testing high-overlap agent pairs present in the roster: e.g., foundry:sw-engineer vs foundry:qa-specialist, foundry:linting-expert vs foundry:sw-engineer, foundry:solution-architect vs foundry:sw-engineer, foundry:web-explorer vs foundry:sw-engineer (look up docs to implement vs implement directly), foundry:challenger vs foundry:sw-engineer (critique plan vs implement). Use only pairs whose two dispatch names occur in the collected roster
- Include exactly 1 `expected_agent: "general-purpose"` problem (general question, no specialist)
- Difficulty distribution: ~40% easy, ~40% medium, ~20% hard (adjust to cover all agents)
- **Auto-invocation coverage**: include ≥3 problems where `task_prompt` uses exact TRIGGER phrasing for an agent with a TRIGGER block (e.g. "what does the requests docs say about retries", "write tests for the auth module", "add docstrings to utils.py") — these are easy/medium; the TRIGGER phrase is the signal
- **SKIP-guard coverage**: include ≥2 problems where `task_prompt` superficially resembles a TRIGGER but a SKIP guard applies — `expected_agent` must be `"general-purpose"` or a different specialist, NOT the TRIGGER agent; add field `"skip_guard_test": true` to these problems
- Add boolean field `"auto_invoke_test": true` to problems covering TRIGGER/SKIP scenarios
- Return valid JSON array only (no prose)

Write JSON array to `.reports/calibrate/<TIMESTAMP>/routing/problems.json`.

### Phase 3 — Run routing selection (parallel)

Read roster from `.reports/calibrate/<TIMESTAMP>/routing/roster.txt`.

Resolve the child batch size from the spawn prompt; an unsubstituted value defaults to 1 child at a time:

```bash
CHILD_BATCH='<CHILD_BATCH>'
case "$CHILD_BATCH" in *'<'*|*'>'*|"") CHILD_BATCH=1;; [1-5]) ;; *) echo "! BLOCKED — child batch must be 1–5"; exit 1;; esac
```

Per problem in `problems.json`, spawn `general-purpose` selector subagent in batches of at most `$CHILD_BATCH` (never more than 5). Read `rules/agent-spawn.md` and budget by effective model: haiku 20, sonnet 8, opus 5 in flight. The orchestrator accounts for all active pipelines and their children within the shared tier ceiling; never independently claim the full ceiling. Dispatch within the child batch size, end the turn, and collect the batch through completion notifications before dispatching the next batch. These limits also cover any overlapping phase.

Each selector gets this prompt (substitute `<ROSTER>`, `<TASK_PROMPT>`, `<PROBLEM_ID>`, `<RUN_DIR>`):

> Select specialized agent for task. Available agents:
>
> ```
> <ROSTER>
> ```
>
> Task: `<TASK_PROMPT>`
>
> Select one agent. If no specialist fits, select `general-purpose`.
>
> Write response to `<RUN_DIR>/selection-<PROBLEM_ID>.md` via Write tool. File must contain ONLY valid JSON (no prose):
>
> `{"selected":"<agent-name>","reasoning":"<one sentence>"}`
>
> Then end reply with exactly one line: `Wrote: <PROBLEM_ID>`

**Context discipline**: subagents write to disk, return single-line ack. Pipeline agent must NOT accumulate full analyses — scorers read from disk in Phase 3. `Wrote: <PROBLEM_ID>` per agent correct.

**Completion handling** — never a poll loop or a waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`); each subagent's result arrives as its `Agent()` result or completion notification (`_FOUNDRY_SHARED/agent-spawn-protocol.md`). Each subagent returns: check for `selection-<PROBLEM_ID>.md`; missing: mark that problem `{"selected":null,"timed_out":true}` with ⏱ in report.

### Phase 4 — Score

<!-- Design note: N=5/10, selection files tiny (~100 bytes), under 2K inline threshold. Inline reading intentional. If N>~20, refactor Phase 4 to use consolidator subagent. -->

Per problem, read `selection-<problem_id>.md` from `.reports/calibrate/<TIMESTAMP>/routing/`. Parse JSON, extract `selected` and `reasoning`. Compare vs `expected_agent` from `problems.json`:

- `selected` == `expected_agent` → `correct: true`, `error_type: null`
- `selected` == `confusion_pair` → `correct: false`, `error_type: "confusion"`
- Other mismatch → `correct: false`, `error_type: "wrong"`
- `timed_out: true` → `correct: false`, `error_type: "timeout"`

Compute aggregates:

- `routing_accuracy` = correct_count / total_count
- `confusion_rate` = confusion_error_count / total_count
- `hard_accuracy` = correct hard / total hard (omit if no hard problems)
- `auto_invoke_accuracy` = correct on `auto_invoke_test: true` problems / total `auto_invoke_test: true` problems (omit if no such problems)
- Confusion list: per incorrect selection, record `(expected → selected, task_prompt, reasoning)`

Verdict:

- `routing_accuracy ≥ 0.90` AND `hard_accuracy ≥ 0.80` → `calibrated`
- `routing_accuracy ≥ 0.80` but below threshold OR `hard_accuracy < 0.80` → `borderline`
- `routing_accuracy < 0.80` → `needs-improvement`

The Write tool refuses any file whose exact basename is `report.md` from a subagent context — use `benchmark-report.md` instead. Write full report to `.reports/calibrate/<TIMESTAMP>/routing/benchmark-report.md`:

```markdown
## Routing Benchmark — <date> — <MODE>

### Per-Problem Results
| Problem ID | Difficulty | Expected | Selected | Correct |
|------------|------------|----------|----------|---------|
| ...

### Aggregate
| Metric           | Value     | Status |
|------------------|-----------|--------|
| Routing accuracy | X/N (XX%) | ≥90% ✓ / 80–90% ~ / <80% ⚠ |
| Hard accuracy    | X/N (XX%) | ≥80% ✓ / <80% ⚠ |
| Confusion errors | N         | 0 ✓ / >0 list pairs |
| Auto-invoke accuracy | X/N (XX%) | ≥90% ✓ / <90% ⚠ (auto_invoke_test problems only) |

### Confused Pairs
| Task Prompt | Expected → Selected | Reasoning |
|-------------|---------------------|-----------|
| ...

(omit this section if no confusion errors)

### Proposals
For each confused pair: suggest specific wording improvements to the relevant agent
descriptions that would disambiguate the routing decision. Reference the NOT-for clause
pattern when applicable — adding "NOT for X" to one agent in the pair is often the
minimal effective fix.
```

Write result JSONL to `.reports/calibrate/<TIMESTAMP>/routing/result.jsonl`:

`{"ts":"<TIMESTAMP>","target":"routing","mode":"<MODE>","routing_accuracy":0.N,"confusion_rate":0.N,"hard_accuracy":0.N,"auto_invoke_accuracy":0.N,"problems":<N>,"verdict":"calibrated|borderline|needs-improvement|incomplete","confused_pairs":["expected→selected",...]}`

### Return value

Return **only** compact JSON (no prose):

`{"target":"routing","routing_accuracy":0.N,"confusion_rate":0.N,"hard_accuracy":0.N,"auto_invoke_accuracy":0.N,"problems":<N>,"verdict":"calibrated|borderline|needs-improvement|incomplete","confused_pairs":["expected→selected",...]}`

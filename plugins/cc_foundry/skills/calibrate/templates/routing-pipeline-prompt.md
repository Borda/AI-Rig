You routing calibration pipeline runner. Complete all phases in sequence.

<!-- Substitutions: TIMESTAMP=run timestamp (YYYY-MM-DDTHH-MM-SSZ), MODE=fast|full, N=problem count (fast=5, full=10) -->

```text
Mode: `<MODE>`
Run dir: `.reports/calibrate/<TIMESTAMP>/routing/`
```

<!-- All paths relative to project root. Pipeline runner must have project root as working dir. -->

### Phase 1 — Collect agent descriptions

Enumerate roster file set. Source tree (`plugins/*/agents/*.md`) is authoritative; installed cache is fallback when source tree carries no agents; project-local `.claude/agents/*.md` is an override tier empty in most setups — `/foundry:setup` never creates that directory, purges stale entries from it, so it must never be the sole source:

```bash
RUN_DIR=".reports/calibrate/<TIMESTAMP>/routing"
mkdir -p "$RUN_DIR"
find plugins -mindepth 3 -maxdepth 3 -path "*/agents/*.md" 2>/dev/null | sort > "$RUN_DIR/roster-files.txt"
if [ ! -s "$RUN_DIR/roster-files.txt" ]; then
    # one version dir per plugin — the cache retains every prior version, so an unfiltered scan duplicates each agent
    for P in ~/.claude/plugins/cache/borda-ai-rig/*/; do
        V=$(find "$P" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | grep -v '\.orphaned_at' | sort -Vr | head -1)
        [ -n "$V" ] && find "$V/agents" -maxdepth 1 -name "*.md" 2>/dev/null
    done | sort > "$RUN_DIR/roster-files.txt"
fi
find .claude/agents -maxdepth 1 -name "*.md" 2>/dev/null >> "$RUN_DIR/roster-files.txt"
grep -c . "$RUN_DIR/roster-files.txt"
```

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

Rules:

- Cover every agent ≥1 in `expected_agent` (distribute evenly given N)
- Include ≥2 hard problems testing high-overlap pairs: e.g., sw-engineer vs qa-specialist, doc-scribe vs oss:shepherd, linting-expert vs sw-engineer, solution-architect vs sw-engineer, web-explorer vs sw-engineer (look up docs to implement vs implement directly), challenger vs sw-engineer (critique plan vs implement), oss:analyse vs oss:review (analyze thread vs code review)
- Include exactly 1 `expected_agent: "general-purpose"` problem (general question, no specialist)
- Difficulty distribution: ~40% easy, ~40% medium, ~20% hard (adjust to cover all agents)
- **Auto-invocation coverage**: include ≥3 problems where `task_prompt` uses exact TRIGGER phrasing for an agent with a TRIGGER block (e.g. "what does the requests docs say about retries", "write tests for the auth module", "add docstrings to utils.py") — these are easy/medium; the TRIGGER phrase is the signal
- **SKIP-guard coverage**: include ≥2 problems where `task_prompt` superficially resembles a TRIGGER but a SKIP guard applies — `expected_agent` must be `"general-purpose"` or a different specialist, NOT the TRIGGER agent; add field `"skip_guard_test": true` to these problems
- Add boolean field `"auto_invoke_test": true` to problems covering TRIGGER/SKIP scenarios
- Return valid JSON array only (no prose)

Write JSON array to `.reports/calibrate/<TIMESTAMP>/routing/problems.json`.

### Phase 3 — Run routing selection (parallel)

Read roster from `.reports/calibrate/<TIMESTAMP>/routing/roster.txt`.

Per problem in `problems.json`, spawn `general-purpose` selector subagent. Issue ALL spawns in **single response** — no waiting between spawns.

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

**Completion handling** — spawns are blocking `Agent()` calls, so no poll loop is possible (`_FOUNDRY_SHARED/agent-spawn-protocol.md` §Synchronous spawns). Each subagent returns: check for `selection-<PROBLEM_ID>.md`; missing: mark that problem `{"selected":null,"timed_out":true}` with ⏱ in report.

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

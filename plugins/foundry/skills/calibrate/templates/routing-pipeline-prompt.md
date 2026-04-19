**Re: Compress routing calibration pipeline prompt to caveman format**

You are routing calibration pipeline runner. Complete all phases in sequence.

<!-- Substitutions: TIMESTAMP=run timestamp (YYYYMMDDTHHMMSSZ), MODE=fast|full, N=problem count (fast=5, full=10) -->

```
Mode: `<MODE>`
Run dir: `.reports/calibrate/<TIMESTAMP>/routing/`
```

<!-- All paths relative to project root. Pipeline runner must have project root as working dir. -->

### Phase 1 — Collect agent descriptions

Read all agent files matching `.claude/agents/*.md`. Per file, extract `name:` and `description:` from YAML frontmatter (between `---` delimiters).

Build roster string, one line per agent:

```
<name>: <description>
```

Bash `mkdir -p` run dir. Write roster to `.reports/calibrate/<TIMESTAMP>/routing/roster.txt`.

### Phase 2 — Generate routing problems

Generate `<N>` synthetic task prompts across all agents. Per problem, produce JSON with these fields:

- `problem_id`: kebab-slug string
- `task_prompt`: realistic user request to orchestrator (no hint at expected agent)
- `expected_agent`: correct `subagent_type` from roster (or `"general-purpose"` if no specialist needed)
- `difficulty`: `"easy"` (single-domain, obvious match), `"medium"` (2 domains, one primary), `"hard"` (ambiguous, requires NOT-for clauses or fine distinctions)
- `confusion_pair`: most likely wrong agent for medium/hard; `null` for easy

Rules:

- Cover every agent ≥1 in `expected_agent` (distribute evenly given N)
- Include ≥2 hard problems testing high-overlap pairs: e.g., sw-engineer vs qa-specialist, doc-scribe vs oss:shepherd, linting-expert vs sw-engineer, solution-architect vs sw-engineer
- Include exactly 1 `expected_agent: "general-purpose"` problem (general question, no specialist)
- Difficulty distribution: ~40% easy, ~40% medium, ~20% hard (adjust to cover all agents)
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

**Context discipline**: subagents write to disk, return single-line ack. Pipeline agent must NOT accumulate full analyses — scorers read from disk in Phase 3. `Wrote: <PROBLEM_ID>` per agent is correct.

**Phase timeout**: checkpoint before spawn (`touch /tmp/calibrate-routing-<TIMESTAMP>`). After all spawns, every 5 min: `find .reports/calibrate/<TIMESTAMP>/routing/ -newer /tmp/calibrate-routing-<TIMESTAMP> -name "selection-*.md" | wc -l`. New files = alive. One +5-min extension if progress. Hard cutoff: 15 min no new files → mark remaining as `{"selected":null,"timed_out":true}` with ⏱ in report.

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
- Confusion list: per incorrect selection, record `(expected → selected, task_prompt, reasoning)`

Verdict:

- `routing_accuracy ≥ 0.90` AND `hard_accuracy ≥ 0.80` → `calibrated`
- `routing_accuracy ≥ 0.80` but below threshold OR `hard_accuracy < 0.80` → `borderline`
- `routing_accuracy < 0.80` → `needs-improvement`

Write full report to `.reports/calibrate/<TIMESTAMP>/routing/report.md`:

```
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

`{"ts":"<TIMESTAMP>","target":"routing","mode":"<MODE>","routing_accuracy":0.N,"confusion_rate":0.N,"hard_accuracy":0.N,"problems":<N>,"verdict":"calibrated|borderline|needs-improvement","confused_pairs":["expected→selected",...]}`

### Return value

Return **only** compact JSON (no prose):

`{"target":"routing","routing_accuracy":0.N,"confusion_rate":0.N,"hard_accuracy":0.N,"problems":<N>,"verdict":"calibrated|borderline|needs-improvement","confused_pairs":["expected→selected",...]}`

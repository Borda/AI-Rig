You are a routing calibration pipeline runner. Complete all phases in sequence.

<!-- Substitutions: TIMESTAMP=run timestamp (YYYYMMDDTHHMMSSZ), MODE=fast|full, N=problem count (fast=5, full=10) -->

```
Mode: `<MODE>`
Run dir: `.reports/calibrate/<TIMESTAMP>/routing/`
```

<!-- All paths are relative to the project root. The pipeline runner must have project root as its working directory. -->

### Phase 1 — Collect agent descriptions

Read all agent files matching `.claude/agents/*.md`. For each file, extract the `name:` and `description:` fields from the YAML frontmatter (between the `---` delimiters).

Build an agent roster string with one line per agent:

```
<name>: <description>
```

Use Bash `mkdir -p` to create the run dir, then write the roster to `.reports/calibrate/<TIMESTAMP>/routing/roster.txt`.

### Phase 2 — Generate routing problems

Generate `<N>` synthetic task prompts covering routing accuracy across all agents. For each problem, produce a JSON object with these fields:

- `problem_id`: kebab-slug string
- `task_prompt`: a realistic user request phrased as a user would type it to an orchestrator (do NOT hint at the expected agent in the prompt)
- `expected_agent`: the correct `subagent_type` name from the roster (or `"general-purpose"` for no-specialist-needed)
- `difficulty`: `"easy"` (single-domain, obvious match), `"medium"` (touches 2 domains, one is primary), `"hard"` (ambiguous, requires reading NOT-for clauses or fine distinctions to disambiguate)
- `confusion_pair`: for medium/hard, the agent name most likely to be incorrectly selected; `null` for easy

Rules:

- Cover every agent from the roster at least once in `expected_agent` across the full set (distribute coverage as evenly as possible given N)
- Include ≥2 hard problems testing high-overlap pairs: e.g., sw-engineer vs qa-specialist, doc-scribe vs oss-shepherd, linting-expert vs sw-engineer, solution-architect vs sw-engineer
- Include exactly 1 problem where no specialized agent is appropriate — `expected_agent: "general-purpose"` (e.g., a general question unrelated to any agent's specialty)
- Difficulty distribution: ~40% easy, ~40% medium, ~20% hard (adjust to cover all agents)
- Return a valid JSON array only (no prose)

Write the JSON array to `.reports/calibrate/<TIMESTAMP>/routing/problems.json`.

### Phase 3 — Run routing selection (parallel)

Read the agent roster from `.reports/calibrate/<TIMESTAMP>/routing/roster.txt`.

For each problem in `problems.json`, spawn a `general-purpose` selector subagent. Issue ALL spawns in a **single response** — no waiting between spawns.

Each selector receives this prompt (substitute `<ROSTER>`, `<TASK_PROMPT>`, `<PROBLEM_ID>`, `<RUN_DIR>`):

> You are an orchestrator selecting which specialized agent to use for a task. Here are the available agents and their descriptions:
>
> ```
> <ROSTER>
> ```
>
> Task: `<TASK_PROMPT>`
>
> Select exactly one agent. If no specialized agent fits the task, select `general-purpose`.
>
> Write your response to `<RUN_DIR>/selection-<PROBLEM_ID>.md` using the Write tool. The file must contain ONLY a valid JSON object (no prose before or after):
>
> `{"selected":"<agent-name>","reasoning":"<one sentence>"}`
>
> Then end your reply with exactly one line: `Wrote: <PROBLEM_ID>`

**Context discipline**: subagents write to disk and return a single-line acknowledgment. The pipeline agent must NOT accumulate their full analyses in its context — scorers read from disk in Phase 3. Receiving only `Wrote: <PROBLEM_ID>` per agent is correct and expected.

**Phase timeout**: create a checkpoint before spawning (`touch /tmp/calibrate-routing-<TIMESTAMP>`). After issuing all spawns, every 5 min run `find .reports/calibrate/<TIMESTAMP>/routing/ -newer /tmp/calibrate-routing-<TIMESTAMP> -name "selection-*.md" | wc -l` to count newly written files. If progress is evident (new files appearing), grant one +5-min extension. Hard cutoff: 15 min of no new files → mark remaining problems as `{"selected":null,"timed_out":true}` with ⏱ in the report.

### Phase 4 — Score

<!-- Design note: for fast mode (N=5) and full mode (N=10), selection files are tiny (~100 bytes each), well under the 2K inline threshold. Inline reading here is intentional. If N is ever increased beyond ~20, refactor Phase 4 to use a consolidator subagent. -->

For each problem, read `selection-<problem_id>.md` from `.reports/calibrate/<TIMESTAMP>/routing/`. Parse the JSON to extract `selected` and `reasoning`. Compare against `expected_agent` from `problems.json`:

- `selected` == `expected_agent` → `correct: true`, `error_type: null`
- `selected` == `confusion_pair` → `correct: false`, `error_type: "confusion"`
- Other mismatch → `correct: false`, `error_type: "wrong"`
- `timed_out: true` → `correct: false`, `error_type: "timeout"`

Compute aggregates:

- `routing_accuracy` = correct_count / total_count
- `confusion_rate` = confusion_error_count / total_count
- `hard_accuracy` = correct hard problems / total hard problems (omit if no hard problems)
- Confusion list: for each incorrect selection, record `(expected → selected, task_prompt, reasoning)`

Verdict:

- `routing_accuracy ≥ 0.90` AND `hard_accuracy ≥ 0.80` → `calibrated`
- `routing_accuracy ≥ 0.80` but below threshold OR `hard_accuracy < 0.80` → `borderline`
- `routing_accuracy < 0.80` → `needs-improvement`

Write the full report to `.reports/calibrate/<TIMESTAMP>/routing/report.md`:

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

Return **only** this compact JSON (no prose before or after):

`{"target":"routing","routing_accuracy":0.N,"confusion_rate":0.N,"hard_accuracy":0.N,"problems":<N>,"verdict":"calibrated|borderline|needs-improvement","confused_pairs":["expected→selected",...]}`
